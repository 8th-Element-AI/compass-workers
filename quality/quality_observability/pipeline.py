"""LocalScorer — production implementation of QualityScorer.

End-to-end shape:

  LocalScorer(config_dict={...} | config_path="configs/runtime.yaml")
    .score_generation(jobs)    -> [{faithfulness, coherence, completeness, meta}]
    .score_retrieval(jobs)     -> [{context_relevance, chunk_utilization, meta}]

The three models (NLI / Embedding / Relevance) are owned as lazy property
slots — first call to a scoring method that needs a particular model
triggers that model's load. A LocalScorer that's only ever asked for
generation scores never loads the relevance reranker, and vice versa.

Both scoring methods are batched: every job in the input list contributes
its NLI pairs / embedding texts / relevance pairs to one flat batch, then
one forward pass per model fans out. This mirrors the toxicity pipeline
and is how the Quality lens amortizes inference across an entire batch
of spans.

numpy is imported lazily inside the scoring methods so the package can be
loaded for `quality-score download` (and the StaticScorer test path) on
environments where numpy/torch/sentence-transformers aren't installed —
e.g. the Docker Stage 1 model-downloader, which is intentionally minimal.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import constants as C
from .config import load_config
from .models import EmbeddingModel, NLIModel, RelevanceModel
from .scorer import QualityScorer
from .text import normalize_output, split_sentences

log = logging.getLogger("quality.pipeline")


class LocalScorer(QualityScorer):
    """Local-model scorer backed by three small CPU-friendly models.

    Args:
        config_path: Path to a runtime.yaml. None -> default location.
        config_dict: Pre-built config dict (compass-workers builds this from
            its Settings). Takes precedence over config_path when both are
            given.
        device: Override the YAML `runtime.device`.
        batch_size: Override the YAML `runtime.batch_size`.

    Either `config_path` or `config_dict` provides the three model
    `local_path` entries, the `recipes:` block (PREMISE_MAX_CHARS etc.),
    and the `runtime:` block (device, batch_size).
    """

    def __init__(
        self,
        config_path: str | None = None,
        *,
        config_dict: dict | None = None,
        device: str | None = None,
        batch_size: int | None = None,
    ) -> None:
        if config_dict is not None:
            self.config = dict(config_dict)
        else:
            self.config = load_config(config_path)

        # ---- runtime block ----
        runtime = self.config.setdefault("runtime", {})
        if device is not None:
            runtime["device"] = device
        if batch_size is not None:
            runtime["batch_size"] = batch_size
        self.device = str(runtime.get("device", "cpu"))
        self.batch_size = int(runtime.get("batch_size", 32))

        # ---- recipes block (defaults from constants.py) ----
        recipes = self.config.setdefault("recipes", {})
        self.premise_max_chars = int(recipes.get("premise_max_chars", C.PREMISE_MAX_CHARS))
        self.max_sents = int(recipes.get("max_sents", C.MAX_SENTS))
        self.sent_min_chars = int(recipes.get("sent_min_chars", C.SENT_MIN_CHARS))
        self.chunk_used_cos = float(recipes.get("chunk_used_cos", C.CHUNK_USED_COS))

        # ---- models block ----
        try:
            self._models_cfg = self.config["models"]
        except KeyError as e:
            raise ValueError("LocalScorer config missing required `models:` block") from e

        # Lazy holders — instantiated in their property getters.
        self._nli: Optional[NLIModel] = None
        self._emb: Optional[EmbeddingModel] = None
        self._rel: Optional[RelevanceModel] = None

    # ------------------------------------------------------------------
    # Lazy model properties
    # ------------------------------------------------------------------
    @property
    def nli(self) -> NLIModel:
        if self._nli is None:
            self._nli = NLIModel(
                self._models_cfg["nli"]["local_path"],
                batch_size=self.batch_size,
                device=self.device,
            )
        return self._nli

    @property
    def emb(self) -> EmbeddingModel:
        if self._emb is None:
            self._emb = EmbeddingModel(
                self._models_cfg["embedding"]["local_path"],
                batch_size=self.batch_size,
                device=self.device,
            )
        return self._emb

    @property
    def rel(self) -> RelevanceModel:
        if self._rel is None:
            self._rel = RelevanceModel(
                self._models_cfg["relevance"]["local_path"],
                batch_size=self.batch_size,
                device=self.device,
            )
        return self._rel

    # ------------------------------------------------------------------
    # Recipe math helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _coverage(src_vecs, tgt_vecs) -> float:
        """Mean over `src` of its best cosine vs `tgt`, clamped to [0, 1].

        Both vector sets must be L2-normalized so the dot product is the
        cosine — EmbeddingModel does that for us. Operations here use the
        arrays' own methods, so numpy doesn't need to be imported in this
        scope.
        """
        sims = src_vecs @ tgt_vecs.T
        return float(min(1.0, max(0.0, sims.max(axis=1).mean())))

    def _split(self, text: str) -> list:
        """split_sentences with this scorer's configured caps."""
        return split_sentences(text, max_sents=self.max_sents,
                               sent_min_chars=self.sent_min_chars)

    # ------------------------------------------------------------------
    # score_generation — faithfulness + coherence + completeness
    # ------------------------------------------------------------------
    def score_generation(self, jobs: list) -> list:
        """Score (input, output) pairs.

        Recipes:
          faithfulness = mean NLI entailment of each output sentence vs
                         the input premise (clipped to premise_max_chars).
          coherence    = 1 - mean NLI contradiction across adjacent output
                         sentence pairs; needs >= 2 sentences.
          completeness = embedding coverage of input sentences by output
                         sentences (cosine-best-match, averaged).

        All three are None when their preconditions don't hold (no input,
        single-sentence output, etc.).
        """
        # numpy is the only hard dep here. Imported lazily so the package
        # stays loadable for `quality-score download` in lean envs.
        import numpy as np

        preps = []
        nli_pairs: list = []
        emb_texts: list = []
        for inp, out in jobs:
            premise = (inp or "")[:self.premise_max_chars]
            out_sents = self._split(normalize_output(out or ""))
            in_sents = self._split(inp or "")

            # Faithfulness pair indices (premise vs each output sentence).
            faith_idx: list = []
            if premise:
                for s in out_sents:
                    faith_idx.append(len(nli_pairs))
                    nli_pairs.append((premise, s))

            # Coherence pair indices (each adjacent pair of output sentences).
            coher_idx: list = []
            for a, b in zip(out_sents, out_sents[1:]):
                coher_idx.append(len(nli_pairs))
                nli_pairs.append((a, b))

            # Embedding-text spans for completeness.
            in_lo = len(emb_texts); emb_texts.extend(in_sents)
            out_lo = len(emb_texts); emb_texts.extend(out_sents)

            preps.append((
                out_sents, in_sents, faith_idx, coher_idx,
                (in_lo, in_lo + len(in_sents)),
                (out_lo, out_lo + len(out_sents)),
            ))

        # One forward pass per model across the whole batch.
        probs = self.nli.predict_proba(nli_pairs) if nli_pairs else np.zeros((0, 3))
        vecs = self.emb.encode(emb_texts) if emb_texts else np.zeros((0, 1))

        entail_idx = self.nli.entail_idx if nli_pairs else 0
        contra_idx = self.nli.contra_idx if nli_pairs else 0

        results = []
        for out_sents, in_sents, faith_idx, coher_idx, (ia, ib), (oa, ob) in preps:
            faith = cohere = complete = None
            meta: dict = {"out_sents": len(out_sents)}

            if faith_idx:
                entail = probs[faith_idx, entail_idx]
                faith = round(float(entail.mean()), 4)
                meta["min_entail"] = round(float(entail.min()), 4)

            if coher_idx:
                contra = probs[coher_idx, contra_idx]
                cohere = round(1.0 - float(contra.mean()), 4)

            if in_sents and out_sents:
                complete = round(self._coverage(vecs[ia:ib], vecs[oa:ob]), 4)

            results.append({
                "faithfulness": faith,
                "coherence":    cohere,
                "completeness": complete,
                "meta":         meta,
            })
        return results

    # ------------------------------------------------------------------
    # score_retrieval — context_relevance + chunk_utilization
    # ------------------------------------------------------------------
    def score_retrieval(self, jobs: list) -> list:
        """Score (query, chunks, answer) triples.

        Recipes:
          context_relevance = mean relevance-cross-encoder score of each
                              chunk against the query (sigmoid'd logit).
          chunk_utilization = fraction of chunks whose best cosine match
                              against the answer clears chunk_used_cos.
                              None when no answer was available.
        """
        import numpy as np

        preps = []
        rel_pairs: list = []
        emb_texts: list = []
        for query, chunks, answer in jobs:
            chunks = [c for c in chunks if c]
            rel_idx: list = []
            if query:
                for c in chunks:
                    rel_idx.append(len(rel_pairs))
                    rel_pairs.append((query, c))

            ans_sents = self._split(normalize_output(answer or ""))

            ch_lo = len(emb_texts); emb_texts.extend(chunks)
            an_lo = len(emb_texts); emb_texts.extend(ans_sents)

            preps.append((
                chunks, rel_idx,
                (ch_lo, ch_lo + len(chunks)),
                (an_lo, an_lo + len(ans_sents)),
            ))

        rels = self.rel.predict_sigmoid(rel_pairs) if rel_pairs else np.zeros(0)
        vecs = self.emb.encode(emb_texts) if emb_texts else np.zeros((0, 1))

        results = []
        for chunks, rel_idx, (ca, cb), (aa, ab) in preps:
            relevance = utilization = None
            meta: dict = {"chunks": len(chunks)}

            if rel_idx:
                scores = rels[rel_idx]
                relevance = round(float(scores.mean()), 4)
                meta["rel"] = [round(float(x), 2) for x in scores]

            if cb > ca and ab > aa:
                best = (vecs[ca:cb] @ vecs[aa:ab].T).max(axis=1)
                used = int((best >= self.chunk_used_cos).sum())
                utilization = round(used / len(chunks), 4)
                meta["used"] = used

            results.append({
                "context_relevance": relevance,
                "chunk_utilization": utilization,
                "meta":              meta,
            })
        return results