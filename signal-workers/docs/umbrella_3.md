# Umbrella 3 — Data Quality (Quality Lens)

> Design doc for the third of four Quality-lens metric umbrellas.
> Status: v1 implemented (mechanical — no models) · Owner: Quality lens worker
> Companion diagram: `umbrella_3_flow.png`

---

## 1. Metrics captured

Record/field-level integrity of **non-LLM data processing** (ETL, extraction,
form processing). Both metrics are **purely mechanical** — computed from span
metadata with no models, no caches, no cross-span lookups. This umbrella runs
even when semantic scoring is disabled (`SIGNAL_QUALITY_SEMANTIC=false`).

| Metric | Type | Range | Question it answers | Failure it catches | Thresholdable |
|---|---|---|---|---|---|
| `data_completeness` | float | 0–1 | What fraction of expected fields/records are present and non-null? | Dropped fields, partial records, incomplete batches | ✅ |
| `data_accuracy` | float | 0–1 | What fraction of values pass validation? | Bad formats, out-of-range values, failed reference checks | ✅ |

**Data contract:**

| Span type | Field | Used for |
|---|---|---|
| `validation` | `metadata.input` (the record, as a dict) | completeness (field-level), accuracy (denominator) |
| `validation` | `metadata.output.errors[]` | accuracy (numerator) |
| `skill_exec` | `metadata.records_processed`, `metadata.batch_size` | completeness (record-level) |

---

## 2. Where this sits in signal-workers

No scorer involvement — the capture logic is two pure helper functions inside
the lens:

```
signal_worker/lenses/quality.py
├── _data_completeness()       ⭐  capture helper (§4)
├── _data_accuracy()           ⭐  capture helper (§4)
└── SPECS: data_completeness (data_op) · data_accuracy (validated_op)
```

Cost profile: effectively free — dict iteration per span, no I/O, no inference.

---

## 3. End-to-end data flow

```
signal_raw_spans (validation, skill_exec spans via the span_type filter)
   ▼
build_context (per span, no batch pre-pass needed)
   ├─ validation:  completeness = non-null fraction of input record fields
   │               accuracy     = 1 − len(output.errors) / field_count
   ├─ skill_exec:  completeness = records_processed / batch_size (clamped to 1)
   ▼
2 specs emit · validation errors → metric_meta (audit)
   ▼
signal_derived_metrics ──(mv_agg_base)──► signal_aggregated_metrics ──► dashboards
```

---

## 4. ⭐ Capture recipes

| Metric | Span type | Recipe | Evidence in `metric_meta` |
|---|---|---|---|
| `data_completeness` | `validation` | `filled_fields / total_fields` of the input record — a value counts as missing if `None`, `""`, `[]`, or `{}` | — |
| `data_completeness` | `skill_exec` | `records_processed / batch_size`, clamped to [0, 1] | — |
| `data_accuracy` | `validation` | `max(0, 1 − len(errors) / field_count)`; `valid=true` with no errors → 1.0 | `errors` (first 20, e.g. `missing_required_field:due_date`) |

### Live-run results (real ClickHouse run, full dataset)

| Metric | Rows | Mean | Reads as |
|---|---|---|---|
| `data_completeness` | 1,418 (709 validation + 709 skill_exec) | 0.9617 | matches the seeded partial records |
| `data_accuracy` | 709 | 0.9958 | exactly the 24 seeded invalid spans |

### Known limitations (by design, documented)

1. **"Expected fields" currently means "whatever the record has"** — there is no
   declared expectation, so a record that *arrives* missing a field entirely
   isn't penalized by completeness (only null/empty values are). A registered
   record schema would make completeness principled — **this collapses into
   Umbrella 4's schema-source decision.**
2. **Two interpretations under one metric name** — field-level (validation) vs
   record-level (skill_exec). Bless or split (`record_completeness`?).
3. **The error→accuracy formula assumes one error ≈ one bad field** — matches
   the current producer's error format (`missing_required_field:due_date`), but
   it's a convention to agree with whoever emits validation spans, not a
   guarantee.

---

## 5. Open decisions

1. **Expected-fields source** — adopt the Umbrella-4 schema registry for
   principled completeness (recommended; one decision serves two umbrellas).
2. **Split or bless** the field-level vs record-level completeness duality.
3. **Error format convention** with the validation-span producer (one error per
   field, `code:field` format) — same conversation as Umbrella 4's metadata
   contract.
