"""Model adapters.

Three classes, each owning one model and its load-time quirks:

  NLIModel        — DeBERTa cross-encoder, softmax over 3 NLI classes;
                    entail/contradiction column indices resolved from the
                    model's own config at load time (no hardcoded order).
  EmbeddingModel  — SentenceTransformer; L2-normalized output so dot
                    product == cosine for downstream coverage math.
  RelevanceModel  — ms-marco cross-encoder reranker; raw logits passed
                    through sigmoid into [0, 1].

All three lazy-load on first call to a `predict*` / `encode` method.
A LocalScorer that never actually needs the relevance model (no
retrieval spans in the batch) never instantiates one — same lazy
pattern as toxicity's ToxicityClassifier.

Threading: sentence-transformers' CrossEncoder.predict is internally
thread-safe for inference (read-only forward pass). The Quality lens
calls these from one worker thread per pod, so no extra locking here.

torch, sentence-transformers, and numpy are imported lazily inside the
methods that need them so the package can be loaded in environments
where they aren't installed — e.g. the Docker Stage 1 model-downloader,
and the StaticScorer test path.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("quality.models")


def resolve_device(requested: str) -> str:
    """Return 'cuda' if asked AND torch reports it available, else 'cpu'.

    Imported lazily so a torch-free environment (StaticScorer-only tests,
    Docker Stage 1 downloader) can still import this module — only call
    resolve_device() when you actually intend to load a model.
    """
    import torch
    return "cuda" if requested == "cuda" and torch.cuda.is_available() else "cpu"


# --------------------------------------------------------------------------
# NLI cross-encoder (DeBERTa) — drives faithfulness + coherence
# --------------------------------------------------------------------------
class NLIModel:
    """3-way NLI cross-encoder. Outputs per-pair softmax over the model's
    3 classes; faithfulness reads the entailment column, coherence reads
    the contradiction column.

    Label order differs by model family (`cross-encoder/nli-*` puts
    contradiction at index 0; MoritzLaurer FEVER models put entailment
    at index 0), so the indices are resolved from `model.config.id2label`
    at load time. Swap NLI models via YAML — no code change needed.
    """

    def __init__(
        self,
        model_path: str,
        *,
        batch_size: int = 32,
        device: str = "cpu",
    ) -> None:
        self.model_path = model_path
        self.batch_size = batch_size
        self.device = device
        self._ce = None
        self._entail_idx: Optional[int] = None
        self._contra_idx: Optional[int] = None

    def _load(self) -> None:
        from sentence_transformers import CrossEncoder
        log.info("[quality] loading NLI model %s (device=%s)",
                 self.model_path, self.device)
        device = resolve_device(self.device)
        self._ce = CrossEncoder(self.model_path, device=device)
        id2label = {
            int(k): str(v).lower()
            for k, v in self._ce.model.config.id2label.items()
        }
        self._entail_idx = next(i for i, lbl in id2label.items() if "entail" in lbl)
        self._contra_idx = next(i for i, lbl in id2label.items() if "contradict" in lbl)
        log.info("[quality] NLI labels %s -> entail=%d contra=%d",
                 id2label, self._entail_idx, self._contra_idx)

    @property
    def entail_idx(self) -> int:
        if self._ce is None:
            self._load()
        return self._entail_idx  # type: ignore[return-value]

    @property
    def contra_idx(self) -> int:
        if self._ce is None:
            self._load()
        return self._contra_idx  # type: ignore[return-value]

    def predict_proba(self, pairs: list):
        """[(premise, hypothesis), ...] -> per-pair softmax probs, shape (N, 3).

        Empty input returns shape (0, 3) so downstream array slicing
        (probs[idx, entail_idx]) doesn't need a special case.
        """
        import numpy as np
        if not pairs:
            return np.zeros((0, 3))
        if self._ce is None:
            self._load()
        logits = self._ce.predict(
            pairs, batch_size=self.batch_size, show_progress_bar=False,
        )
        logits = np.asarray(logits, dtype="float64")
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        return e / e.sum(axis=1, keepdims=True)


# --------------------------------------------------------------------------
# Embedding model — drives completeness + chunk_utilization
# --------------------------------------------------------------------------
class EmbeddingModel:
    """SentenceTransformer with normalize_embeddings=True so dot product
    == cosine. The pipeline uses that property for the `_coverage()` math."""

    def __init__(
        self,
        model_path: str,
        *,
        batch_size: int = 32,
        device: str = "cpu",
    ) -> None:
        self.model_path = model_path
        self.batch_size = batch_size
        self.device = device
        self._model = None

    def _load(self) -> None:
        from sentence_transformers import SentenceTransformer
        log.info("[quality] loading embedding model %s (device=%s)",
                 self.model_path, self.device)
        device = resolve_device(self.device)
        self._model = SentenceTransformer(self.model_path, device=device)

    def encode(self, texts: list):
        """Encode texts to L2-normalized vectors. Empty input -> (0, 1)."""
        import numpy as np
        if not texts:
            return np.zeros((0, 1))
        if self._model is None:
            self._load()
        return self._model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )


# --------------------------------------------------------------------------
# Relevance cross-encoder — drives context_relevance
# --------------------------------------------------------------------------
class RelevanceModel:
    """ms-marco-style relevance reranker. Logits -> sigmoid -> [0, 1]."""

    def __init__(
        self,
        model_path: str,
        *,
        batch_size: int = 32,
        device: str = "cpu",
    ) -> None:
        self.model_path = model_path
        self.batch_size = batch_size
        self.device = device
        self._ce = None

    def _load(self) -> None:
        from sentence_transformers import CrossEncoder
        log.info("[quality] loading relevance model %s (device=%s)",
                 self.model_path, self.device)
        device = resolve_device(self.device)
        self._ce = CrossEncoder(self.model_path, device=device)

    def predict_sigmoid(self, pairs: list):
        """[(query, passage), ...] -> sigmoid(logit) in [0, 1]. Empty -> (0,)."""
        import numpy as np
        if not pairs:
            return np.zeros(0)
        if self._ce is None:
            self._load()
        logits = self._ce.predict(
            pairs, batch_size=self.batch_size, show_progress_bar=False,
        )
        return 1.0 / (1.0 + np.exp(-np.asarray(logits, dtype="float64")))