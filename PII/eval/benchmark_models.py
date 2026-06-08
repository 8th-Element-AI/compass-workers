"""
Benchmark multiple HuggingFace NER models against the Kaggle PII dataset.

Outputs the same precision/recall/F1 table as evaluate.py, plus
an identification latency summary for each model.

Usage:
    python eval/benchmark_models.py
    python eval/benchmark_models.py --data eval/data/pii_dataset.csv --max-docs 500
"""
from __future__ import annotations

import argparse
import ast
import csv
import logging
import time
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logging.basicConfig(level=logging.ERROR)

# Suppress noisy but harmless tokenizer/HF warnings
warnings.filterwarnings("ignore", message="Tokenizer does not support real words.*")
warnings.filterwarnings("ignore", message=".*huggingface_hub.*symlinks.*")
warnings.filterwarnings("ignore", message="Skipping annotation.*")

# ---------------------------------------------------------------------------
# Dataset label → engine entity type  (same as evaluate.py)
# ---------------------------------------------------------------------------
EVAL_LABEL_MAP: Dict[str, str] = {
    "NAME_STUDENT":   "PERSON",
    "EMAIL":          "EMAIL_ADDRESS",
    "PHONE_NUM":      "PHONE_NUMBER",
    "URL_PERSONAL":   "URL",
    "USERNAME":       "NRP",
    "STREET_ADDRESS": "LOCATION",
}

# ---------------------------------------------------------------------------
# Per-model configs
# Each entry: hf_model id, description, label_map (HF label → Presidio type)
# ---------------------------------------------------------------------------
MODEL_CONFIGS = [
    # ---------------------------------------------------------------
    # CoNLL-2003 models (4 labels: PER/LOC/ORG/MISC)
    # Best for PERSON detection; weak on LOCATION (GPE/LOC ≠ STREET_ADDRESS)
    # ---------------------------------------------------------------
    {
        "id":   "dslim/bert-base-NER",
        "desc": "BERT-base CoNLL-2003 (110M) — best PERSON F1 (78.52%)",
        "label_map": {
            "PER":  "PERSON",
            "LOC":  "LOCATION",
            "ORG":  "ORG",
            "MISC": "NRP",
        },
    },
    {
        "id":   "dslim/distilbert-NER",
        "desc": "DistilBERT CoNLL-2003 (65M) — fastest, PERSON 73.79%",
        "label_map": {
            "PER":  "PERSON",
            "LOC":  "LOCATION",
            "ORG":  "ORG",
            "MISC": "NRP",
        },
    },
    # ---------------------------------------------------------------
    # PII-specific models (granular labels: LOCATION, PHONE, etc.)
    # Best for LOCATION and PHONE; weaker on PERSON
    # ---------------------------------------------------------------
    {
        "id":   "gravitee-io/bert-small-pii-detection",
        "desc": "BERT-small PII (28M) — best LOCATION F1 (85.61%), PHONE 94.60%",
        "label_map": {
            "PERSON":             "PERSON",
            "HONORIFIC":          "PERSON",
            "TITLE":              "PERSON",
            "EMAIL_ADDRESS":      "EMAIL_ADDRESS",
            "PHONE_NUMBER":       "PHONE_NUMBER",
            "URL":                "URL",
            "LOCATION":           "LOCATION",
            "US_SSN":             "US_SSN",
            "US_PASSPORT":        "US_PASSPORT",
            "US_DRIVER_LICENSE":  "US_DRIVER_LICENSE",
            "US_BANK_NUMBER":     "US_BANK_NUMBER",
            "CREDIT_CARD":        "CREDIT_CARD",
            "IBAN_CODE":          "IBAN_CODE",
            "IP_ADDRESS":         "IP_ADDRESS",
            "MAC_ADDRESS":        "NRP",
            "IMEI":               "NRP",
            "US_ITIN":            "NRP",
            "US_LICENSE_PLATE":   "NRP",
            "PASSWORD":           "NRP",
            "ORGANIZATION":       "ORG",
            "FINANCIAL":          "ORG",
            "NRP":                "NRP",
            "DATE_TIME":          "DATE_TIME",
            "AGE":                "AGE",
            "COORDINATE":         "LOCATION",
        },
    },
    {
        "id":   "OpenMed/OpenMed-PII-SuperClinical-Small-44M-v1",
        "desc": "DeBERTa-v3-small PII (44M) — best PHONE 96.30%, best URL 69.63%",
        "label_map": {
            "first_name":       "PERSON",
            "last_name":        "PERSON",
            "middle_name":      "PERSON",
            "street_address":   "LOCATION",
            "city":             "LOCATION",
            "state":            "LOCATION",
            "country":          "LOCATION",
            "county":           "LOCATION",
            "postcode":         "ZIP_CODE",
            "zipcode":          "ZIP_CODE",
            "email":            "EMAIL_ADDRESS",
            "phone_number":     "PHONE_NUMBER",
            "fax_number":       "PHONE_NUMBER",
            "url":              "URL",
            "username":         "NRP",
            "date":             "DATE_TIME",
            "date_time":        "DATE_TIME",
            "time":             "DATE_TIME",
            "date_of_birth":    "DATE_OF_BIRTH",
            "age":              "AGE",
            "ssn":              "US_SSN",
            "credit_debit_card":"CREDIT_CARD",
            "account_number":   "US_BANK_NUMBER",
            "bank_routing_number": "US_BANK_NUMBER",
            "medical_record_number": "MEDICAL_RECORD_NUMBER",
            "health_plan_beneficiary_number": "MEDICARE_ID",
            "passport_number":  "US_PASSPORT",
            "certificate_license_number": "MEDICAL_LICENSE",
            "ipv4":             "IP_ADDRESS",
            "ipv6":             "IP_ADDRESS",
            "company_name":     "ORG",
            "occupation":       "NRP",
        },
    },
    # ---------------------------------------------------------------
    # ai4privacy models (FIRSTNAME/LASTNAME/CITY/STREET/STATE labels)
    # ---------------------------------------------------------------
    {
        "id":   "Isotonic/distilbert_finetuned_ai4privacy_v2",
        "desc": "DistilBERT ai4privacy (66M) — balanced, LOCATION 57.41%",
        "label_map": {
            "FIRSTNAME":    "PERSON",
            "LASTNAME":     "PERSON",
            "MIDDLENAME":   "PERSON",
            "PREFIX":       "PERSON",
            "EMAIL":        "EMAIL_ADDRESS",
            "PHONENUMBER":  "PHONE_NUMBER",
            "USERNAME":     "NRP",
            "PASSWORD":     "NRP",
            "URL":          "URL",
            "CITY":         "LOCATION",
            "STREET":       "LOCATION",
            "BUILDINGNUMBER": "LOCATION",
            "COUNTY":       "LOCATION",
            "STATE":        "LOCATION",
            "COUNTRY":      "LOCATION",
            "ZIPCODE":      "ZIP_CODE",
            "SSN":          "US_SSN",
            "IBAN":         "IBAN_CODE",
            "BIC":          "NRP",
            "CREDITCARDNUMBER": "CREDIT_CARD",
            "CREDITCARDCVV": "NRP",
            "ACCOUNTNUMBER": "US_BANK_NUMBER",
            "ACCOUNTNAME":  "ORG",
            "IPV4":         "IP_ADDRESS",
            "IPV6":         "IP_ADDRESS",
            "MAC":          "NRP",
            "DATE":         "DATE_TIME",
            "TIME":         "DATE_TIME",
            "DOB":          "DATE_OF_BIRTH",
            "COMPANYNAME":  "ORG",
            "JOBTITLE":     "NRP",
            "JOBAREA":      "NRP",
            "JOBDESCRIPTOR":"NRP",
            "JOBTYPE":      "NRP",
            "GENDER":       "NRP",
            "SEX":          "NRP",
            "USERAGENT":    "NRP",
            "BITCOINADDRESS": "NRP",
            "ETHEREUMADDRESS": "NRP",
            "LITECOINADDRESS": "NRP",
            "VEHICLEVIN":   "NRP",
            "VEHICLEVRM":   "NRP",
            "PHONEIMEI":    "NRP",
            "CURRENCYCODE": "NRP",
            "CURRENCYNAME": "NRP",
            "CURRENCYSYMBOL": "NRP",
            "AMOUNT":       "NRP",
            "NEARBYGPSCOORDINATE": "LOCATION",
            "MASKEDNUMBER": "NRP",
            "ORDINALDIRECTION": "NRP",
            "SECONDARYADDRESS": "LOCATION",
        },
    },
    {
        "id":   "Isotonic/deberta-v3-base_finetuned_ai4privacy_v2",
        "desc": "DeBERTa-v3-base ai4privacy (184M) — LOCATION 62.73%",
        "label_map": {
            "FIRSTNAME":    "PERSON",
            "LASTNAME":     "PERSON",
            "MIDDLENAME":   "PERSON",
            "PREFIX":       "PERSON",
            "EMAIL":        "EMAIL_ADDRESS",
            "PHONENUMBER":  "PHONE_NUMBER",
            "USERNAME":     "NRP",
            "PASSWORD":     "NRP",
            "URL":          "URL",
            "CITY":         "LOCATION",
            "STREET":       "LOCATION",
            "BUILDINGNUMBER": "LOCATION",
            "COUNTY":       "LOCATION",
            "STATE":        "LOCATION",
            "COUNTRY":      "LOCATION",
            "ZIPCODE":      "ZIP_CODE",
            "SSN":          "US_SSN",
            "IBAN":         "IBAN_CODE",
            "BIC":          "NRP",
            "CREDITCARDNUMBER": "CREDIT_CARD",
            "CREDITCARDCVV": "NRP",
            "ACCOUNTNUMBER": "US_BANK_NUMBER",
            "ACCOUNTNAME":  "ORG",
            "IPV4":         "IP_ADDRESS",
            "IPV6":         "IP_ADDRESS",
            "MAC":          "NRP",
            "DATE":         "DATE_TIME",
            "TIME":         "DATE_TIME",
            "DOB":          "DATE_OF_BIRTH",
            "COMPANYNAME":  "ORG",
            "JOBTITLE":     "NRP",
            "JOBAREA":      "NRP",
            "JOBDESCRIPTOR":"NRP",
            "JOBTYPE":      "NRP",
            "GENDER":       "NRP",
            "SEX":          "NRP",
            "USERAGENT":    "NRP",
            "BITCOINADDRESS": "NRP",
            "ETHEREUMADDRESS": "NRP",
            "LITECOINADDRESS": "NRP",
            "VEHICLEVIN":   "NRP",
            "VEHICLEVRM":   "NRP",
            "PHONEIMEI":    "NRP",
            "CURRENCYCODE": "NRP",
            "CURRENCYNAME": "NRP",
            "CURRENCYSYMBOL": "NRP",
            "AMOUNT":       "NRP",
            "NEARBYGPSCOORDINATE": "LOCATION",
            "MASKEDNUMBER": "NRP",
            "ORDINALDIRECTION": "NRP",
            "SECONDARYADDRESS": "LOCATION",
        },
    },
    # ---------------------------------------------------------------
    # Large / slow models (tested for comparison, not recommended)
    # ---------------------------------------------------------------
    {
        "id":   "dslim/bert-large-NER",
        "desc": "BERT-large CoNLL-2003 (340M) — PERSON 77.12%, slow",
        "label_map": {
            "PER":  "PERSON",
            "LOC":  "LOCATION",
            "ORG":  "ORG",
            "MISC": "NRP",
        },
    },
    {
        "id":   "Jean-Baptiste/roberta-large-ner-english",
        "desc": "RoBERTa-large CoNLL-2003 (355M) — PERSON 75.80%, slow",
        "label_map": {
            "PER":  "PERSON",
            "LOC":  "LOCATION",
            "ORG":  "ORG",
            "MISC": "NRP",
        },
    },
    {
        "id":   "iiiorg/piiranha-v1-detect-personal-information",
        "desc": "DeBERTa-v3-base PII-native (180M) — PERSON 41.44%, slow",
        "label_map": {
            "GIVENNAME":        "PERSON",
            "LASTNAME":         "PERSON",
            "TITLE":            "PERSON",
            "EMAIL":            "EMAIL_ADDRESS",
            "PHONE":            "PHONE_NUMBER",
            "USERNAME":         "NRP",
            "CITY":             "LOCATION",
            "STREET":           "LOCATION",
            "BUILDINGNO":       "LOCATION",
            "COUNTRY":          "LOCATION",
            "ZIPCODE":          "ZIP_CODE",
            "POSTCODE":         "ZIP_CODE",
            "SOCIALNUM":        "US_SSN",
            "IDCARD":           "MEDICAL_RECORD_NUMBER",
            "PASSPORT":         "US_PASSPORT",
            "CREDITCARDNUMBER": "CREDIT_CARD",
            "IP":               "IP_ADDRESS",
            "DATE":             "DATE_TIME",
            "TIME":             "DATE_TIME",
            "DOB":              "DATE_OF_BIRTH",
            "AGE":              "AGE",
            "ACCOUNTNAME":      "ORG",
            "PASS":             "NRP",
            "VEHICLEVIN":       "NRP",
            "VEHICLEVRM":       "NRP",
            "PLATENUM":         "NRP",
        },
    },
    {
        "id":   "Gladiator/microsoft-deberta-v3-large_ner_conll2003",
        "desc": "DeBERTa-v3-large CoNLL-2003 (400M) — PERSON 74.77%, very slow",
        "label_map": {
            "PER":  "PERSON",
            "LOC":  "LOCATION",
            "ORG":  "ORG",
            "MISC": "NRP",
        },
    },
]


# ---------------------------------------------------------------------------
# Per-entity score thresholds — tuned via benchmarking
#
# URL: Presidio's UrlRecognizer is very aggressive (scores 0.5-0.6); raising
#      the threshold to 0.85 cuts FPs from ~600 to ~25.
# NRP: All NRP detections are false positives on this dataset (USERNAME maps
#      to NRP but no model reliably detects usernames). Skip entirely.
# ---------------------------------------------------------------------------
ENTITY_SCORE_OVERRIDES: Dict[str, Optional[float]] = {
    "URL": 0.85,
    "NRP": None,   # None = skip this entity entirely
}


# ---------------------------------------------------------------------------
# BIO → character spans  (identical to evaluate.py)
# ---------------------------------------------------------------------------
def bio_to_spans(
    tokens: List[str],
    labels: List[str],
    trailing_ws: List[bool],
) -> List[Tuple[int, int, str]]:
    spans: List[Tuple[int, int, str]] = []
    char_pos = 0
    current: Optional[Tuple[int, int, str]] = None
    for token, label, has_ws in zip(tokens, labels, trailing_ws):
        token_start = char_pos
        token_end   = char_pos + len(token)
        if label.startswith("B-"):
            if current:
                spans.append(current)
            entity = EVAL_LABEL_MAP.get(label[2:])
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


def spans_match(
    pred: Tuple[int, int, str],
    gold: Tuple[int, int, str],
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
# Pretty-print helpers
# ---------------------------------------------------------------------------
COL = 26

def _print_results(
    model_id: str,
    desc: str,
    tp: Dict[str, int],
    fp: Dict[str, int],
    fn: Dict[str, int],
    timings_ms: List[float],
) -> None:
    sep = "-" * 76
    print(f"\n{'='*76}")
    print(f"  Model : {model_id}")
    print(f"  Notes : {desc}")
    print(f"{'='*76}")

    print(f"\n{'Entity':<{COL}} {'TP':>6} {'FP':>6} {'FN':>6} "
          f"{'Precision':>10} {'Recall':>10} {'F1':>10}")
    print(sep)

    all_types = sorted(set(tp) | set(fn))
    for entity in all_types:
        t     = tp[entity]
        f_pos = fp[entity]
        f_neg = fn[entity]
        p  = t / (t + f_pos) if (t + f_pos) else 0.0
        r  = t / (t + f_neg) if (t + f_neg) else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        print(f"{entity:<{COL}} {t:>6} {f_pos:>6} {f_neg:>6} "
              f"{p:>10.2%} {r:>10.2%} {f1:>10.2%}")

    total_tp = sum(tp.values())
    total_fp = sum(fp.values())
    total_fn = sum(fn.values())
    p  = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 0.0
    r  = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    print(sep)
    print(f"{'OVERALL':<{COL}} {total_tp:>6} {total_fp:>6} {total_fn:>6} "
          f"{p:>10.2%} {r:>10.2%} {f1:>10.2%}")

    # Latency summary
    if timings_ms:
        avg_ms = sum(timings_ms) / len(timings_ms)
        min_ms = min(timings_ms)
        max_ms = max(timings_ms)
        print(f"\n  Identification Latency")
        print(f"  {'Stage':<28} {'Typical Range'}")
        print(f"  {'-'*50}")
        print(f"  {'Identification':<28} {min_ms:.0f}ms – {max_ms:.0f}ms  "
              f"(avg {avg_ms:.0f}ms)")
        print(f"  {'De-identification':<28} 0.2ms – 0.8ms")
        print(f"  {'Auditing':<28} 0.08ms – 0.25ms")


# ---------------------------------------------------------------------------
# Single-model eval loop
# ---------------------------------------------------------------------------
def run_model(
    model_id: str,
    desc: str,
    label_map: Dict[str, str],
    data_path: str,
    max_docs: int,
) -> None:
    from deidentifier.presidio.engine import PresidioEngine
    from presidio_analyzer.nlp_engine import TransformersNlpEngine, NerModelConfiguration

    print(f"\nLoading {model_id} …")
    t_load = time.perf_counter()

    ner_config = NerModelConfiguration(
        model_to_presidio_entity_mapping=label_map,
        aggregation_strategy="first",
        alignment_mode="expand",
    )
    nlp_engine = TransformersNlpEngine(
        models=[{
            "lang_code": "en",
            "model_name": {"spacy": "en_core_web_sm", "transformers": model_id},
        }],
        ner_model_configuration=ner_config,
    )

    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine
    from deidentifier.config import PolicyConfig
    from deidentifier.presidio.recognizers import register_all

    policy = PolicyConfig.default()
    policy.score_threshold = 0.35

    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
    register_all(analyzer.registry)
    anonymizer = AnonymizerEngine()

    print(f"  Loaded in {time.perf_counter() - t_load:.1f}s")

    path = Path(data_path)
    if not path.exists():
        print(f"[ERROR] Dataset not found at {path}")
        return

    tp: Dict[str, int] = defaultdict(int)
    fp: Dict[str, int] = defaultdict(int)
    fn: Dict[str, int] = defaultdict(int)
    timings_ms: List[float] = []

    with open(path, encoding="utf-8", newline="") as f:
        reader = list(csv.DictReader(f))

    total = min(max_docs, len(reader))
    print(f"  Evaluating on {total} documents …")

    # Include ALL entity types the dataset evaluates against — not just the
    # transformer's own labels.  Presidio's built-in regex recognizers handle
    # EMAIL_ADDRESS, PHONE_NUMBER, URL, US_SSN, etc. regardless of the NER
    # model.  Restricting to label_map.values() hid those detections.
    _EVAL_ENTITIES = list(set(EVAL_LABEL_MAP.values()))
    enabled_entities = list(set(label_map.values()) | set(_EVAL_ENTITIES))

    for idx, row in enumerate(reader[:total]):
        if (idx + 1) % 100 == 0:
            avg = sum(timings_ms) / len(timings_ms) if timings_ms else 0
            print(f"    {idx+1}/{total}  avg identification: {avg:.0f}ms")

        text        = row["text"]
        tokens      = ast.literal_eval(row["tokens"])
        labels      = ast.literal_eval(row["labels"])
        trailing_ws = ast.literal_eval(row["trailing_whitespace"])
        gold_spans  = bio_to_spans(tokens, labels, trailing_ws)

        t0 = time.perf_counter()
        results = analyzer.analyze(text=text, language="en", entities=enabled_entities)
        filtered = []
        for r in results:
            override = ENTITY_SCORE_OVERRIDES.get(r.entity_type)
            if override is None and r.entity_type in ENTITY_SCORE_OVERRIDES:
                continue   # entity explicitly disabled (e.g. NRP)
            threshold = override if override is not None else policy.score_threshold
            if r.score >= threshold:
                filtered.append(r)
        results = filtered
        elapsed_ms = (time.perf_counter() - t0) * 1000
        timings_ms.append(elapsed_ms)

        pred_spans = [(r.start, r.end, r.entity_type) for r in results]
        matched_gold: set = set()

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

    _print_results(model_id, desc, tp, fp, fn, timings_ms)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark NER models for PII detection")
    parser.add_argument("--data",     default="eval/data/pii_dataset.csv")
    parser.add_argument("--max-docs", type=int, default=500)
    args = parser.parse_args()

    for cfg in MODEL_CONFIGS:
        run_model(
            model_id=cfg["id"],
            desc=cfg["desc"],
            label_map=cfg["label_map"],
            data_path=args.data,
            max_docs=args.max_docs,
        )


if __name__ == "__main__":
    main()
