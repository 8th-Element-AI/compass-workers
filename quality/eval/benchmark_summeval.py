"""Benchmark coherence + completeness models against SummEval.

SummEval: 100 source articles × 16 machine summaries, each with human ratings
(1–5) on `coherence` and `relevance` (= coverage of important source content,
which is what our `completeness` proxy targets). We score every (source, summary)
with the production recipes and compare to the human ratings.

  coherence    → NLI contradiction over adjacent summary sentences (NLI family)
  completeness → embedding coverage of source sentences by the summary (embed family)

Graded ratings are best assessed by **Spearman correlation** (the SummEval
standard); we report that as primary, plus median-split P/R/F1 + AUROC so the
numbers line up with the faithfulness/relevance tables.

Usage:
    python eval/benchmark_summeval.py
    python eval/benchmark_summeval.py --metric coherence
"""
from __future__ import annotations

import argparse
import time
import warnings

import numpy as np

warnings.filterwarnings("ignore")

from quality_observability.text import normalize_output, split_sentences  # noqa: E402

NLI_MODELS = [
    {"id": "cross-encoder/nli-deberta-v3-xsmall", "desc": "current NLI (~22M)"},
    {"id": "cross-encoder/nli-deberta-v3-base", "desc": "larger same family (~184M)"},
    {"id": "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli", "desc": "FEVER+ANLI NLI (~184M)"},
]
EMBED_MODELS = [
    {"id": "sentence-transformers/all-MiniLM-L6-v2", "desc": "current embedder (~22M)"},
    {"id": "sentence-transformers/all-mpnet-base-v2", "desc": "stronger SBERT (~110M)"},
    {"id": "BAAI/bge-base-en-v1.5", "desc": "top-MTEB embedder (~109M)"},
]


def load_examples():
    from huggingface_hub import hf_hub_download
    import pandas as pd
    path = hf_hub_download("mteb/summeval",
                           "data/test-00000-of-00001-35901af5f6649399.parquet",
                           repo_type="dataset")
    df = pd.read_parquet(path)
    ex = []   # (source, summary, coherence, relevance)
    for _, row in df.iterrows():
        src = row["text"]
        for summ, coh, rel in zip(row["machine_summaries"],
                                  row["coherence"], row["relevance"]):
            ex.append((src, summ, float(coh), float(rel)))
    return ex


# ---------- coherence: NLI contradiction over adjacent summary sentences ----------
def coherence_scores(model_id, examples):
    from sentence_transformers import CrossEncoder
    ce = CrossEncoder(model_id)
    id2label = {int(k): str(v).lower() for k, v in ce.model.config.id2label.items()}
    contra_idx = next(i for i, lbl in id2label.items() if "contradict" in lbl)

    nli_pairs, groups = [], []
    for _, summ, _, _ in examples:
        sents = split_sentences(normalize_output(summ or ""))
        idx = []
        for a, b in zip(sents, sents[1:]):
            idx.append(len(nli_pairs))
            nli_pairs.append([a, b])
        groups.append(idx)

    t0 = time.perf_counter()
    logits = ce.predict(nli_pairs, batch_size=32, show_progress_bar=False) if nli_pairs else np.zeros((0, 3))
    elapsed = time.perf_counter() - t0
    logits = np.asarray(logits, dtype="float64")
    e = np.exp(logits - logits.max(axis=1, keepdims=True))
    probs = e / e.sum(axis=1, keepdims=True)
    # 1 - mean contradiction; single-sentence summaries (no pairs) → NaN, dropped later
    scores = np.array([1.0 - float(probs[idx, contra_idx].mean()) if idx else np.nan
                       for idx in groups])
    return scores, elapsed


# ---------- completeness: embedding coverage of source by summary ----------
def completeness_scores(model_id, examples):
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(model_id)

    # encode all unique sentence lists in one batch per side
    all_texts, spans = [], []
    for src, summ, _, _ in examples:
        in_sents = split_sentences(src or "")
        out_sents = split_sentences(normalize_output(summ or ""))
        ia = len(all_texts); all_texts.extend(in_sents)
        oa = len(all_texts); all_texts.extend(out_sents)
        spans.append((ia, ia + len(in_sents), oa, oa + len(out_sents)))

    t0 = time.perf_counter()
    vecs = (model.encode(all_texts, batch_size=64, normalize_embeddings=True,
                         show_progress_bar=False) if all_texts else np.zeros((0, 1)))
    elapsed = time.perf_counter() - t0
    vecs = np.asarray(vecs)

    scores = []
    for ia, ib, oa, ob in spans:
        if ib > ia and ob > oa:
            sims = vecs[ia:ib] @ vecs[oa:ob].T
            scores.append(float(min(1.0, max(0.0, sims.max(axis=1).mean()))))
        else:
            scores.append(np.nan)
    return np.array(scores), elapsed


def evaluate(scores, ratings):
    from sklearn.metrics import (roc_auc_score,
                                 precision_recall_fscore_support)
    from scipy.stats import spearmanr
    scores = np.asarray(scores); ratings = np.asarray(ratings)
    ok = ~np.isnan(scores)
    scores, ratings = scores[ok], ratings[ok]
    spear = spearmanr(scores, ratings).correlation
    # median split of human ratings → balanced binary task
    labels = (ratings >= np.median(ratings)).astype(int)
    best = (0.0, 0.0, 0.0, -1.0)
    for t in np.unique(scores):
        pred = (scores >= t).astype(int)
        p, r, f, _ = precision_recall_fscore_support(labels, pred, average="binary", zero_division=0)
        if f > best[3]:
            best = (float(t), p, r, f)
    auroc = roc_auc_score(labels, scores) if len(set(labels)) > 1 else float("nan")
    return spear, best, auroc, len(scores)


def run(metric, examples, models, score_fn, rating_idx):
    print(f"\n########## {metric} ##########")
    rows = []
    for cfg in models:
        print(f"→ {cfg['id']}  ({cfg['desc']})")
        try:
            scores, secs = score_fn(cfg["id"], examples)
        except Exception as e:
            print(f"   FAILED: {e}\n"); continue
        ratings = [e[rating_idx] for e in examples]
        spear, (th, p, r, f1), auroc, n = evaluate(scores, ratings)
        ms = 1000 * secs / max(1, len(examples))
        rows.append((cfg["id"], spear, p, r, f1, auroc, th, ms, n))
        print(f"   Spearman={spear:.3f}  F1={f1:.3f} P={p:.3f} R={r:.3f} "
              f"AUROC={auroc:.3f} @thr={th:.2f}  ({ms:.1f} ms/ex, n={n})\n")
    print("=" * 104)
    print(f"{metric:42} {'Spearman':>9} {'P':>6} {'R':>6} {'F1':>6} {'AUROC':>7} {'thr':>5} {'ms/ex':>7}")
    print("-" * 104)
    for mid, spear, p, r, f1, auroc, th, ms, n in rows:
        print(f"{mid:42} {spear:>9.3f} {p:>6.3f} {r:>6.3f} {f1:>6.3f} "
              f"{auroc:>7.3f} {th:>5.2f} {ms:>7.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metric", choices=["coherence", "completeness", "both"], default="both")
    args = ap.parse_args()
    examples = load_examples()
    print(f"Loaded {len(examples)} (source, summary) examples from SummEval")
    if args.metric in ("coherence", "both"):
        run("coherence", examples, NLI_MODELS, coherence_scores, rating_idx=2)
    if args.metric in ("completeness", "both"):
        run("completeness", examples, EMBED_MODELS, completeness_scores, rating_idx=3)


if __name__ == "__main__":
    main()