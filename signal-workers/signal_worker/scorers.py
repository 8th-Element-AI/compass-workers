"""Quality scorers — local-model scoring for the semantic Quality metrics.

Mirrors the PricingSource shape: a small interface (`QualityScorer`), a
production implementation backed by local models (`LocalScorer`), and a
deterministic `StaticScorer` for tests. The Quality lens depends only on the
interface, so the backend can be swapped (e.g. for an LLM judge) without
touching the specs.

`LocalScorer` uses three small CPU-friendly models, lazy-loaded on first use
(so mechanical-only runs never import torch):

  NLI        MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli  entailment / contradiction
  Embedding  sentence-transformers/all-MiniLM-L6-v2        sentence vectors
  Relevance  cross-encoder/ms-marco-MiniLM-L-6-v2          query<->passage relevance

The NLI label order is read from the model config at load time (see nli()), so
swapping NLI models needs no code change — the FEVER+ANLI default was chosen by
benchmarking (docs/QUALITY_BENCHMARK_RESULTS.md).

Scoring recipes (documented proxies — coarser than an LLM judge, see
ARCHITECTURE.md):

  faithfulness       mean NLI entailment of each output sentence against the
                     span's input text (the grounding context)
  coherence          1 − mean NLI contradiction across adjacent output
                     sentence pairs; needs ≥2 sentences to be assessable
  completeness      mean over input sentences of their best cosine match
                     among output sentences (coverage of the input by the output)
  context_relevance  mean relevance-cross-encoder score of each retrieved
                     chunk against the query
  chunk_utilization  fraction of chunks whose best cosine match against the
                     answer clears CHUNK_USED_COS
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("signal.worker.quality.scorer")

# JSON outputs are flattened to "key is value." lines before sentence-level
# scoring — NLI models are trained on natural language, not raw JSON.
PREMISE_MAX_CHARS = 2000   # truncate grounding text fed to the NLI model
MAX_SENTS = 10             # cap sentences per side; keeps pair counts bounded
SENT_MIN_CHARS = 3
CHUNK_USED_COS = 0.5       # cosine above this = "the answer used this chunk"

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def split_sentences(text: str, max_sents: int = MAX_SENTS) -> list:
    if not text:
        return []
    sents = [s.strip() for s in _SENT_SPLIT.split(text)]
    return [s for s in sents if len(s) >= SENT_MIN_CHARS][:max_sents]


def _flatten_json(obj, prefix="") -> list:
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                lines.extend(_flatten_json(v, f"{prefix}{k}."))
            else:
                lines.append(f"{prefix}{k} is {v}.")
    elif isinstance(obj, list):
        for v in obj[:20]:
            if isinstance(v, (dict, list)):
                lines.extend(_flatten_json(v, prefix))
            else:
                lines.append(f"{prefix}{v}.")
    return lines


def normalize_output(text: str) -> str:
    """JSON-looking output -> 'key is value.' sentences; anything else unchanged."""
    if not text:
        return ""
    t = text.strip()
    if t[:1] not in "{[":
        return text
    try:
        obj = json.loads(t)
    except Exception:
        return text
    flat = _flatten_json(obj)
    return "\n".join(flat) if flat else text


class QualityScorer:
    """Interface. Both methods take a batch of jobs and return one dict per job."""

    def score_generation(self, jobs: list) -> list:
        """jobs: [(input_text, output_text)] ->
        [{"faithfulness": float|None, "coherence": float|None,
          "completeness": float|None, "meta": dict}]"""
        raise NotImplementedError

    def score_retrieval(self, jobs: list) -> list:
        """jobs: [(query, [chunk_text], answer_text|None)] ->
        [{"context_relevance": float|None, "chunk_utilization": float|None,
          "meta": dict}]"""
        raise NotImplementedError


class StaticScorer(QualityScorer):
    """Fixed mid-range scores — offline tests without torch installed."""

    def __init__(self, value: float = 0.75):
        self.value = value

    def score_generation(self, jobs):
        return [{"faithfulness": self.value, "coherence": self.value,
                 "completeness": self.value, "meta": {"scorer": "static"}}
                for _ in jobs]

    def score_retrieval(self, jobs):
        return [{"context_relevance": self.value,
                 "chunk_utilization": self.value if ans else None,
                 "meta": {"scorer": "static"}}
                for (_, _, ans) in jobs]


class LocalScorer(QualityScorer):
    """Local-model scorer. Models lazy-load on first scoring call; every batch
    of jobs is flattened into one predict()/encode() call per model."""

    def __init__(self, nli_model: str, embed_model: str, relevance_model: str,
                 batch_size: int = 32):
        self.nli_model = nli_model
        self.embed_model = embed_model
        self.relevance_model = relevance_model
        self.batch_size = batch_size
        self._nli = None
        self._emb = None
        self._rel = None
        # NLI label order differs by model family (cross-encoder/nli-* put
        # contradiction at 0, MoritzLaurer FEVER models put entailment at 0), so
        # the entailment/contradiction column indices are read from the loaded
        # model's config rather than hardcoded — this is what lets any NLI model
        # drop in (see docs/QUALITY_BENCHMARK_RESULTS.md).
        self._entail_idx = None
        self._contra_idx = None

    # ---- lazy models (mirrors the Safety lens's lazy PresidioEngine) ----
    def nli(self):
        if self._nli is None:
            from sentence_transformers import CrossEncoder
            log.info("[quality] loading NLI model %s", self.nli_model)
            self._nli = CrossEncoder(self.nli_model)
            id2label = {int(k): str(v).lower()
                        for k, v in self._nli.model.config.id2label.items()}
            self._entail_idx = next(i for i, l in id2label.items() if "entail" in l)
            self._contra_idx = next(i for i, l in id2label.items() if "contradict" in l)
            log.info("[quality] NLI labels %s -> entail=%d contra=%d",
                     id2label, self._entail_idx, self._contra_idx)
        return self._nli

    def emb(self):
        if self._emb is None:
            from sentence_transformers import SentenceTransformer
            log.info("[quality] loading embedding model %s", self.embed_model)
            self._emb = SentenceTransformer(self.embed_model)
        return self._emb

    def rel(self):
        if self._rel is None:
            from sentence_transformers import CrossEncoder
            log.info("[quality] loading relevance model %s", self.relevance_model)
            self._rel = CrossEncoder(self.relevance_model)
        return self._rel

    # ---- model call helpers ----
    def _nli_probs(self, pairs: list):
        """[(premise, hypothesis)] -> per-pair softmax probs over the model's 3 NLI
        classes; read columns via self._entail_idx / self._contra_idx (label order
        is model-dependent and resolved in nli())."""
        import numpy as np
        if not pairs:
            return np.zeros((0, 3))
        logits = self.nli().predict(pairs, batch_size=self.batch_size,
                                    show_progress_bar=False)
        logits = np.asarray(logits, dtype="float64")
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        return e / e.sum(axis=1, keepdims=True)

    def _rel_scores(self, pairs: list):
        """[(query, passage)] -> sigmoid(logit) in 0..1."""
        import numpy as np
        if not pairs:
            return np.zeros(0)
        logits = self.rel().predict(pairs, batch_size=self.batch_size,
                                    show_progress_bar=False)
        return 1.0 / (1.0 + np.exp(-np.asarray(logits, dtype="float64")))

    def _encode(self, texts: list):
        import numpy as np
        if not texts:
            return np.zeros((0, 1))
        return self.emb().encode(texts, batch_size=self.batch_size,
                                 normalize_embeddings=True,
                                 show_progress_bar=False)

    @staticmethod
    def _coverage(src_vecs, tgt_vecs) -> float:
        """Mean over src of its best cosine vs tgt, clamped to 0..1
        (vectors are L2-normalized, so dot product = cosine)."""
        sims = src_vecs @ tgt_vecs.T
        return float(min(1.0, max(0.0, sims.max(axis=1).mean())))

    # ---- generation: faithfulness / coherence / completeness ----
    def score_generation(self, jobs: list) -> list:
        preps, nli_pairs, emb_texts = [], [], []
        for inp, out in jobs:
            prem = (inp or "")[:PREMISE_MAX_CHARS]
            out_sents = split_sentences(normalize_output(out or ""))
            in_sents = split_sentences(inp or "")
            faith_idx = []
            if prem:
                for s in out_sents:
                    faith_idx.append(len(nli_pairs))
                    nli_pairs.append((prem, s))
            coher_idx = []
            for a, b in zip(out_sents, out_sents[1:]):
                coher_idx.append(len(nli_pairs))
                nli_pairs.append((a, b))
            in_lo = len(emb_texts); emb_texts.extend(in_sents)
            out_lo = len(emb_texts); emb_texts.extend(out_sents)
            preps.append((out_sents, in_sents, faith_idx, coher_idx,
                          (in_lo, in_lo + len(in_sents)),
                          (out_lo, out_lo + len(out_sents))))

        probs = self._nli_probs(nli_pairs)
        vecs = self._encode(emb_texts)

        results = []
        for out_sents, in_sents, faith_idx, coher_idx, (ia, ib), (oa, ob) in preps:
            faith = cohere = complete = None
            meta = {"out_sents": len(out_sents)}
            if faith_idx:
                entail = probs[faith_idx, self._entail_idx]
                faith = round(float(entail.mean()), 4)
                meta["min_entail"] = round(float(entail.min()), 4)
            if coher_idx:
                contra = probs[coher_idx, self._contra_idx]
                cohere = round(1.0 - float(contra.mean()), 4)
            if in_sents and out_sents:
                complete = round(self._coverage(vecs[ia:ib], vecs[oa:ob]), 4)
            results.append({"faithfulness": faith, "coherence": cohere,
                            "completeness": complete, "meta": meta})
        return results

    # ---- retrieval: context_relevance / chunk_utilization ----
    def score_retrieval(self, jobs: list) -> list:
        preps, rel_pairs, emb_texts = [], [], []
        for query, chunks, answer in jobs:
            chunks = [c for c in chunks if c]
            rel_idx = []
            if query:
                for c in chunks:
                    rel_idx.append(len(rel_pairs))
                    rel_pairs.append((query, c))
            ans_sents = split_sentences(normalize_output(answer or ""))
            ch_lo = len(emb_texts); emb_texts.extend(chunks)
            an_lo = len(emb_texts); emb_texts.extend(ans_sents)
            preps.append((chunks, rel_idx,
                          (ch_lo, ch_lo + len(chunks)),
                          (an_lo, an_lo + len(ans_sents))))

        rels = self._rel_scores(rel_pairs)
        vecs = self._encode(emb_texts)

        results = []
        for chunks, rel_idx, (ca, cb), (aa, ab) in preps:
            relevance = utilization = None
            meta = {"chunks": len(chunks)}
            if rel_idx:
                scores = rels[rel_idx]
                relevance = round(float(scores.mean()), 4)
                meta["rel"] = [round(float(x), 2) for x in scores]
            if cb > ca and ab > aa:
                best = (vecs[ca:cb] @ vecs[aa:ab].T).max(axis=1)
                used = int((best >= CHUNK_USED_COS).sum())
                utilization = round(used / len(chunks), 4)
                meta["used"] = used
            results.append({"context_relevance": relevance,
                            "chunk_utilization": utilization, "meta": meta})
        return results
