"""
Evaluate the Presidio de-identification engine against the Kaggle PII dataset.

Dataset: https://www.kaggle.com/datasets/alejopaullier/pii-external-dataset

Setup:
    pip install kaggle
    # Place kaggle.json in C:\\Users\\<you>\\.kaggle\\kaggle.json
    kaggle datasets download alejopaullier/pii-external-dataset -p eval/data --unzip

Run:
    python eval/evaluate.py
    python eval/evaluate.py --data eval/data/pii_dataset.csv --max-docs 1000
"""
from __future__ import annotations

import argparse
import ast
import csv
import logging
import sys
from collections import defaultdict
from pathlib import Path

logging.basicConfig(level=logging.ERROR)

class _TimingsOnly(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "Timings" in record.getMessage()

_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(name)s — %(message)s"))
_handler.addFilter(_TimingsOnly())
_engine_logger = logging.getLogger("deidentifier.presidio.engine")
_engine_logger.setLevel(logging.INFO)
_engine_logger.addHandler(_handler)
_engine_logger.propagate = False

# ---------------------------------------------------------------------------
# Dataset label → engine entity type
# ---------------------------------------------------------------------------
LABEL_MAP: dict[str, str] = {
    "NAME_STUDENT":   "PERSON",
    "EMAIL":          "EMAIL_ADDRESS",
    "PHONE_NUM":      "PHONE_NUMBER",
    "URL_PERSONAL":   "URL",
    "USERNAME":       "NRP",
    "STREET_ADDRESS": "LOCATION",
}


# ---------------------------------------------------------------------------
# BIO tokens → character-level spans
# Uses trailing_whitespace to reconstruct exact character positions.
# ---------------------------------------------------------------------------
def bio_to_spans(
    tokens: list[str],
    labels: list[str],
    trailing_ws: list[bool],
) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    char_pos = 0
    current: tuple[int, int, str] | None = None

    for token, label, has_ws in zip(tokens, labels, trailing_ws):
        token_start = char_pos
        token_end   = char_pos + len(token)

        if label.startswith("B-"):
            if current:
                spans.append(current)
            entity = LABEL_MAP.get(label[2:])
            current = (token_start, token_end, entity) if entity else None

        elif label.startswith("I-") and current:
            current = (current[0], token_end, current[2])

        else:
            if current:
                spans.append(current)
            current = None

        char_pos = token_end + (1 if has_ws else 0)

    if current:
        spans.append(current)

    return spans


# ---------------------------------------------------------------------------
# Span overlap matching (IoU)
# ---------------------------------------------------------------------------
def spans_match(
    pred: tuple[int, int, str],
    gold: tuple[int, int, str],
    min_iou: float = 0.5,
) -> bool:
    p_start, p_end, p_type = pred
    g_start, g_end, g_type = gold
    if p_type != g_type:
        return False
    overlap = max(0, min(p_end, g_end) - max(p_start, g_start))
    union   = max(p_end, g_end) - min(p_start, g_start)
    return (overlap / union) >= min_iou if union > 0 else False


# ---------------------------------------------------------------------------
# Main evaluation loop
# ---------------------------------------------------------------------------
def evaluate(
    data_path: str,
    max_docs: int = 500,
    output_path: str = "eval/output.json",
) -> None:
    import json

    path = Path(data_path)
    if not path.exists():
        print(
            f"[ERROR] Dataset not found at {path}\n\n"
            "Download it first:\n"
            "  pip install kaggle\n"
            "  kaggle datasets download alejopaullier/pii-external-dataset "
            "-p eval/data --unzip\n"
        )
        sys.exit(1)

    from deidentifier.presidio.engine import PresidioEngine
    engine = PresidioEngine.get_instance()

    tp: dict[str, int] = defaultdict(int)
    fp: dict[str, int] = defaultdict(int)
    fn: dict[str, int] = defaultdict(int)

    with open(path, encoding="utf-8", newline="") as f:
        reader = list(csv.DictReader(f))

    total = min(max_docs, len(reader))
    print(f"Evaluating on {total} documents …\n")

    output_records = []

    for idx, row in enumerate(reader[:total]):
        if (idx + 1) % 50 == 0:
            print(f"  {idx + 1}/{total} documents processed …")

        text        = row["text"]
        tokens      = ast.literal_eval(row["tokens"])
        labels      = ast.literal_eval(row["labels"])
        trailing_ws = ast.literal_eval(row["trailing_whitespace"])

        gold_spans = bio_to_spans(tokens, labels, trailing_ws)
        result     = engine.process(text)
        pred_spans = [
            (e.start, e.end, e.entity_type)
            for e in result.audit_record.entries
        ]

        matched_gold: set[int] = set()

        for pred in pred_spans:
            hit = False
            for i, gold in enumerate(gold_spans):
                if i not in matched_gold and spans_match(pred, gold):
                    tp[pred[2]] += 1
                    matched_gold.add(i)
                    hit = True
                    break
            if not hit:
                fp[pred[2]] += 1

        for i, gold in enumerate(gold_spans):
            if i not in matched_gold:
                fn[gold[2]] += 1

        output_records.append({
            "document_id":       row["document"],
            "original_text":     text,
            "deidentified_text": result.deidentified_text,
            "entities": [
                {
                    "entity_type":   e.entity_type,
                    "original_text": text[e.start:e.end],
                    "start":         e.start,
                    "end":           e.end,
                    "score":         round(e.score, 4),
                    "strategy":      e.strategy,
                }
                for e in result.audit_record.entries
            ],
        })

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output_records, indent=2), encoding="utf-8")
    print(f"\nDetailed output written to: {out.resolve()}\n")

    _print_results(tp, fp, fn)


# ---------------------------------------------------------------------------
# Results table
# ---------------------------------------------------------------------------
def _print_results(
    tp: dict[str, int],
    fp: dict[str, int],
    fn: dict[str, int],
) -> None:
    all_types = sorted(set(tp) | set(fn))
    col = 26

    print(f"\n{'Entity':<{col}} {'TP':>6} {'FP':>6} {'FN':>6} "
          f"{'Precision':>10} {'Recall':>10} {'F1':>10}")
    print("-" * 76)

    for entity in all_types:
        t     = tp[entity]
        f_pos = fp[entity]
        f_neg = fn[entity]
        p  = t / (t + f_pos) if (t + f_pos) else 0.0
        r  = t / (t + f_neg) if (t + f_neg) else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        print(f"{entity:<{col}} {t:>6} {f_pos:>6} {f_neg:>6} "
              f"{p:>10.2%} {r:>10.2%} {f1:>10.2%}")

    total_tp = sum(tp.values())
    total_fp = sum(fp.values())
    total_fn = sum(fn.values())
    p  = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 0.0
    r  = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0

    print("-" * 76)
    print(f"{'OVERALL':<{col}} {total_tp:>6} {total_fp:>6} {total_fn:>6} "
          f"{p:>10.2%} {r:>10.2%} {f1:>10.2%}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate de-identification engine")
    parser.add_argument(
        "--data",
        default="eval/data/pii_dataset.csv",
        help="Path to pii_dataset.csv",
    )
    parser.add_argument(
        "--max-docs",
        type=int,
        default=500,
        help="Maximum number of documents to evaluate (default: 500)",
    )
    parser.add_argument(
        "--output",
        default="eval/output.json",
        help="Path to write detailed per-document output (default: eval/output.json)",
    )
    args = parser.parse_args()
    evaluate(args.data, args.max_docs, args.output)
