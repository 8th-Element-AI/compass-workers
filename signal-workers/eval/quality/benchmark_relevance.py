"""Benchmark context_relevance reranker models against MS MARCO (v1.1 validation).

Mirrors benchmark_faithfulness.py. MS MARCO gives, per query, several passages
each flagged `is_selected` (1 = relevant). We build balanced (query, passage)
pairs and score each with the production relevance recipe
(`signal_worker/scorers.py` uses a cross-encoder + sigmoid per (query, chunk)),
then report threshold-swept P/R/F1 + AUROC/AUPRC per model.

Note: MS MARCO is the *training distribution* for the ms-marco-* rerankers, so
expect near-ceiling scores for them (in-distribution). bge-reranker is trained on
other data and is the fairer out-of-distribution comparison.

Usage:
    python eval/quality/benchmark_relevance.py --max-queries 400
"""
from __future__ import annotations

import argparse
import time
import warnings

import numpy as np

warnings.filterwarnings("ignore")

MODELS = [
    {"id": "cross-encoder/ms-marco-MiniLM-L-6-v2",
     "desc": "current production reranker (~22M)"},
    {"id": "cross-encoder/ms-marco-MiniLM-L-12-v2",
     "desc": "deeper, same family (~33M)"},
    {"id": "BAAI/bge-reranker-base",
     "desc": "SOTA-class reranker, out-of-distribution (~278M)"},
]


def load_pairs(max_queries: int, seed: int = 0):
    from huggingface_hub import hf_hub_download
    import pandas as pd
    path = hf_hub_download("microsoft/ms_marco",
                           "v1.1/validation-00000-of-00001.parquet",
                           repo_type="dataset")
    df = pd.read_parquet(path)
    rng = np.random.default_rng(seed)
    pairs = []   # (query, passage, label)
    for _, row in df.head(max_queries).iterrows():
        q = row["query"]
        pas = row["passages"]
        texts = list(pas["passage_text"])
        sel = list(pas["is_selected"])
        pos = [t for t, s in zip(texts, sel) if s == 1]
        neg = [t for t, s in zip(texts, sel) if s == 0]
        if not pos or not neg:
            continue
        # balance: equal positives and negatives per query
        k = min(len(pos), len(neg))
        neg_sample = list(rng.choice(neg, size=k, replace=False))
        for t in pos[:k]:
            pairs.append((q, t, 1))
        for t in neg_sample:
            pairs.append((q, t, 0))
    return pairs


def score_model(model_id: str, pairs):
    """Score (query, passage) relevance with a cross-encoder reranker → sigmoid,
    exactly as the production relevance recipe does per (query, chunk)."""
    from sentence_transformers import CrossEncoder
    ce = CrossEncoder(model_id)
    ce_pairs = [[q, p] for q, p, _ in pairs]
    t0 = time.perf_counter()
    logits = ce.predict(ce_pairs, batch_size=32, show_progress_bar=False)
    elapsed = time.perf_counter() - t0
    logits = np.asarray(logits, dtype="float64").reshape(-1)
    scores = 1.0 / (1.0 + np.exp(-logits))   # sigmoid → 0..1, like production
    return scores, elapsed


def evaluate(scores, labels):
    from sklearn.metrics import (roc_auc_score, average_precision_score,
                                 precision_recall_fscore_support)
    labels = np.asarray(labels)
    best = (0.0, 0.0, 0.0, -1.0)
    for t in np.unique(scores):
        pred = (scores >= t).astype(int)
        p, r, f, _ = precision_recall_fscore_support(
            labels, pred, average="binary", zero_division=0)
        if f > best[3]:
            best = (float(t), p, r, f)
    return best, roc_auc_score(labels, scores), average_precision_score(labels, scores)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-queries", type=int, default=400)
    ap.add_argument("--models", nargs="*", default=None)
    args = ap.parse_args()

    pairs = load_pairs(args.max_queries)
    labels = [lbl for _, _, lbl in pairs]
    print(f"Loaded {len(pairs)} balanced pairs "
          f"({sum(labels)} relevant / {len(labels)-sum(labels)} not)\n")

    configs = [m for m in MODELS if not args.models or m["id"] in args.models]
    rows = []
    for cfg in configs:
        print(f"→ {cfg['id']}  ({cfg['desc']})")
        try:
            scores, secs = score_model(cfg["id"], pairs)
        except Exception as e:
            print(f"   FAILED: {e}\n")
            continue
        (th, p, r, f1), auroc, auprc = evaluate(scores, labels)
        ms = 1000 * secs / len(pairs)
        rows.append((cfg["id"], p, r, f1, auroc, auprc, th, ms))
        print(f"   F1={f1:.3f} P={p:.3f} R={r:.3f} AUROC={auroc:.3f} "
              f"AUPRC={auprc:.3f} @thr={th:.2f}  ({ms:.1f} ms/pair)\n")

    print("=" * 100)
    print(f"{'model':46} {'P':>6} {'R':>6} {'F1':>6} {'AUROC':>7} {'AUPRC':>7} {'thr':>5} {'ms/pair':>8}")
    print("-" * 100)
    for mid, p, r, f1, auroc, auprc, th, ms in rows:
        print(f"{mid:46} {p:>6.3f} {r:>6.3f} {f1:>6.3f} {auroc:>7.3f} "
              f"{auprc:>7.3f} {th:>5.2f} {ms:>8.1f}")


if __name__ == "__main__":
    main()
