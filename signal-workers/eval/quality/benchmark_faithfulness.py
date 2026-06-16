"""Benchmark faithfulness scoring models against HaluEval (QA split).

Mirrors PII/eval/benchmark_models.py: load a labeled dataset, run each candidate
model through the SAME production faithfulness recipe, and print
precision/recall/F1 (+ AUROC/AUPRC + latency) per model.

Faithfulness recipe (identical to signal_worker/scorers.py · score_generation):
  premise   = context (input), capped at PREMISE_MAX_CHARS
  hypothesis = each sentence of the output (JSON flattened first)
  score      = mean NLI entailment probability across output sentences

HaluEval QA gives, per record: {knowledge, question, right_answer,
hallucinated_answer}. We build two labeled pairs per record:
    (knowledge, right_answer)        -> faithful      (label 1)
    (knowledge, hallucinated_answer) -> hallucinated  (label 0)
so the set is balanced and needs no manual labeling.

P/R/F1 needs a binary prediction from the 0-1 score, so we sweep the decision
threshold and report the best-F1 operating point, plus the threshold-free
AUROC / AUPRC (the honest model-vs-model comparison).

Usage:
    python eval/quality/benchmark_faithfulness.py --max-records 300
    python eval/quality/benchmark_faithfulness.py --models cross-encoder/nli-deberta-v3-xsmall
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import warnings
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore")

# Reuse the EXACT production recipe helpers so the benchmark measures what the
# lens computes, not a re-implementation.
_PKG = Path(__file__).resolve().parents[2]          # signal-workers/
sys.path.insert(0, str(_PKG))
from signal_worker.scorers import split_sentences, normalize_output, PREMISE_MAX_CHARS  # noqa: E402

HALU_URL = "https://raw.githubusercontent.com/RUCAIBox/HaluEval/main/data/qa_data.json"
DATA_CACHE = Path(__file__).parent / "data" / "halueval_qa.json"

# Candidate NLI models. Label order is read from each model's config at runtime
# (cross-encoder/nli-* use contradiction/entailment/neutral; MoritzLaurer FEVER
# models use entailment/neutral/contradiction) — we never hardcode the index.
MODELS = [
    {"id": "cross-encoder/nli-deberta-v3-xsmall",
     "desc": "current production NLI (~22M)"},
    {"id": "cross-encoder/nli-deberta-v3-base",
     "desc": "same family, larger (~184M)"},
    {"id": "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",
     "desc": "FEVER+ANLI-trained NLI (~184M) — purpose-relevant"},
]


def load_pairs(max_records: int):
    DATA_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_CACHE.exists():
        print(f"Downloading HaluEval QA -> {DATA_CACHE} …")
        urllib.request.urlretrieve(HALU_URL, DATA_CACHE)
    pairs = []   # (context, answer, label)
    with open(DATA_CACHE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ex = json.loads(line)
            ctx = ex["knowledge"]
            pairs.append((ctx, ex["right_answer"], 1))
            pairs.append((ctx, ex["hallucinated_answer"], 0))
            if len(pairs) >= 2 * max_records:
                break
    return pairs


def score_model(model_id: str, pairs):
    """Run the production faithfulness recipe with `model_id` as the NLI model.
    Returns (scores array, total_seconds)."""
    from sentence_transformers import CrossEncoder
    ce = CrossEncoder(model_id)
    id2label = {int(k): str(v).lower() for k, v in ce.model.config.id2label.items()}
    entail_idx = next(i for i, lbl in id2label.items() if "entail" in lbl)

    # Flatten all (premise, hypothesis-sentence) NLI pairs; remember grouping.
    nli_pairs, groups = [], []
    for ctx, ans, _ in pairs:
        prem = (ctx or "")[:PREMISE_MAX_CHARS]
        sents = split_sentences(normalize_output(ans or ""))
        idx = []
        for s in sents:
            idx.append(len(nli_pairs))
            nli_pairs.append([prem, s])
        groups.append(idx)

    t0 = time.perf_counter()
    logits = (ce.predict(nli_pairs, batch_size=32, show_progress_bar=False)
              if nli_pairs else np.zeros((0, 3)))
    elapsed = time.perf_counter() - t0

    logits = np.asarray(logits, dtype="float64")
    e = np.exp(logits - logits.max(axis=1, keepdims=True))
    probs = e / e.sum(axis=1, keepdims=True)

    scores = np.array([float(probs[idx, entail_idx].mean()) if idx else 0.0
                       for idx in groups])
    return scores, elapsed


def evaluate(scores, labels):
    from sklearn.metrics import (roc_auc_score, average_precision_score,
                                 precision_recall_fscore_support)
    labels = np.asarray(labels)
    best = (0.0, 0.0, 0.0, -1.0)   # threshold, P, R, F1
    for t in np.unique(scores):
        pred = (scores >= t).astype(int)
        p, r, f, _ = precision_recall_fscore_support(
            labels, pred, average="binary", zero_division=0)
        if f > best[3]:
            best = (float(t), p, r, f)
    auroc = roc_auc_score(labels, scores)
    auprc = average_precision_score(labels, scores)
    return best, auroc, auprc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-records", type=int, default=300,
                    help="HaluEval records (×2 labeled pairs)")
    ap.add_argument("--models", nargs="*", default=None,
                    help="subset of model ids to run")
    args = ap.parse_args()

    pairs = load_pairs(args.max_records)
    labels = [lbl for _, _, lbl in pairs]
    print(f"Loaded {len(pairs)} labeled pairs "
          f"({sum(labels)} faithful / {len(labels)-sum(labels)} hallucinated)\n")

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
    print(f"{'model':48} {'P':>6} {'R':>6} {'F1':>6} {'AUROC':>7} {'AUPRC':>7} {'thr':>5} {'ms/pair':>8}")
    print("-" * 100)
    for mid, p, r, f1, auroc, auprc, th, ms in rows:
        print(f"{mid:48} {p:>6.3f} {r:>6.3f} {f1:>6.3f} {auroc:>7.3f} "
              f"{auprc:>7.3f} {th:>5.2f} {ms:>8.1f}")


if __name__ == "__main__":
    main()
