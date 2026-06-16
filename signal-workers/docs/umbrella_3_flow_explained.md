# Umbrella 3 — Data Quality Flow (Explained)

> Companion to `umbrella_3_flow.png`. A demo-friendly walkthrough of how the
> Quality lens grades **plain data processing** (ETL, extraction, validation) —
> the non-LLM plumbing. Grounded in the actual code
> (`signal_worker/lenses/quality.py`). See also the design doc `umbrella_3.md`.

## The one-sentence pitch

> "Umbrellas 1 and 2 graded LLM behavior with ML models. Umbrella 3 grades the
> boring-but-critical data plumbing — are the records complete, did the values
> pass validation? It's purely mechanical: no models, no cache, just arithmetic
> on a single span's metadata. Effectively free, and it runs even when all
> semantic scoring is switched off."

## The big picture (top to bottom)

Same assembly-line shape, but the middle is much simpler: there's **no orange
scorer box** because no models run. The flow branches at the top on span type,
does a little counting in `build_context`, and emits rows. That's it.

---

## What umbrella 3 measures

Record/field-level integrity of **non-LLM data processing**:

| Metric | Plain-English question | What it catches | Thresholdable |
|---|---|---|---|
| **`data_completeness`** | "What fraction of expected fields/records are present and filled in?" | Dropped fields, partial records, batches that only half-finished. | ✅ |
| **`data_accuracy`** | "What fraction of values passed validation?" | Bad formats, out-of-range values, failed reference checks. | ✅ |

> Demo framing: "This is the cheap, reliable umbrella. No GPU, no inference —
> just counting fields. It runs even with `SIGNAL_QUALITY_SEMANTIC=false`."

**Cost profile:** effectively free — dict iteration per span, no I/O, no
inference, no cross-span lookups.

---

## The one structural quirk: two span types feed completeness

The diagram branches at the top on `span_type?`. That's because
**`data_completeness` is computed two different ways** depending on what produced
the span (code: `quality.py` lines 184–195):

**Path A — `validation` spans (field-level):**

- `data_completeness` = filled fields ÷ total fields of the input record.
- A field counts as "missing" if it's `None`, `""`, `[]`, or `{}` (the
  `EMPTYISH` set).
- Example: a record with 10 fields, 1 of them blank → **0.9**.

**Path B — `skill_exec` spans (record-level):**

- `data_completeness` = `records_processed` ÷ `batch_size`, clamped to ≤ 1.0.
- Example: a job told to process 1,000 records but only got through 960 →
  **0.96**.

> Worth flagging in the demo (a documented open question): these are **two
> different meanings under one metric name** — "fraction of fields filled" vs
> "fraction of records processed." The team may eventually split them (e.g. into
> `record_completeness`).

---

## How `data_accuracy` works (`quality.py` lines 198–205)

Only computed for `validation` spans. The validator emits an `errors[]` list, and
the formula is:

> `data_accuracy = max(0, 1 − number_of_errors / field_count)`

- A 10-field record with 1 validation error → **0.9**.
- Validation passed clean (no errors) → **1.0**.
- Clamped so it can't go negative.

**Evidence saved:** the first 20 errors go into `metric_meta` (e.g.
`missing_required_field:due_date`), so a low score on a dashboard is traceable to
the exact failures.

> The honest caveat: this formula **assumes one error ≈ one bad field**. That
> matches the current producer's error format, but it's a *convention* agreed
> with whoever emits validation spans, not a guarantee.

---

## Walking through the boxes

**1. `signal_raw_spans` (blue cylinder, top)**
The immutable ClickHouse source. The fetch pulls `validation` and `skill_exec`
span types for this umbrella.

**2. `fetch_batch` (gray arrow)**
Same watermark/bookmark mechanism — only grabs spans newer than the last
processed point.

**3. `span_type?` (the diamond)**
The branch point: `validation` spans go one way, `skill_exec` spans the other.

**4. `build_context` (per span, no models, no cache)**
- **validation:** computes `data_completeness` (filled/total fields) **and**
  `data_accuracy` (1 − errors/fields).
- **skill_exec:** computes `data_completeness` (records_processed / batch_size).

  There's no batch pre-pass here — unlike umbrellas 1 and 2, nothing needs to be
  scored ahead of time, so each span is handled independently and immediately.

**5. `2 MetricSpecs emit via ctx_value()`**
`data_completeness` (on `data_op` spans) and `data_accuracy` (on `validated_op`
spans). Validation errors ride along into `metric_meta` for audit.

**6. `signal_derived_metrics` (blue cylinder)**
One row per metric per span (EAV layout), bulk-inserted per batch, then the
watermark advances.

**7. `mv_agg_base` → `signal_aggregated_metrics`**
Materialized view auto-fires on insert, rolls into 1-minute buckets.

**8. `avgMerge` → dashboards / KPIs / signals**
Merged at read time; Postgres thresholds fire alerts.

---

## The biggest documented limitation (forward-links to umbrella 4)

Right now, **"expected fields" just means "whatever the record happens to
have."** There's no declared schema, so a record that arrives *missing a field
entirely* isn't penalized — only fields that are present-but-empty count against
completeness.

The fix is a **registered record schema** — and the doc explicitly notes this
**collapses into umbrella 4's schema-source decision** (the dashed purple
"future" box in the diagram). One decision would make both umbrellas more
principled.

Other documented open items:

- **Split or bless** the field-level vs record-level completeness duality.
- **Error-format convention** with the validation-span producer (one error per
  field, `code:field` format) — the same conversation as umbrella 4's metadata
  contract.

---

## Live-run sanity check (credibility point)

On the real full dataset:

| Metric | Rows | Mean | Reads as |
|---|---|---|---|
| `data_completeness` | 1,418 (709 validation + 709 skill_exec) | 0.9617 | matches the seeded partial records |
| `data_accuracy` | 709 | 0.9958 | exactly the 24 seeded invalid spans |

The numbers line up precisely with what was deliberately broken in the test data
— the metric measures real defects, not noise.

---

## One-glance summary table

| Metric | Span type | Recipe | Evidence |
|---|---|---|---|
| **data_completeness** | `validation` | filled / total fields of the record (`EMPTYISH` = missing) | — |
| **data_completeness** | `skill_exec` | records_processed / batch_size, clamped to [0,1] | — |
| **data_accuracy** | `validation` | max(0, 1 − errors / field_count) | first 20 `errors` |

---

## The 3 themes to hit in your demo

1. **What it measures:** completeness and accuracy of plain data processing —
   the non-AI plumbing that AI systems quietly depend on.
2. **Why it's different:** purely mechanical, zero models, effectively free, and
   runs even with semantic scoring disabled.
3. **Where it's honest about its limits:** completeness currently can't see
   fully-missing fields without a declared schema — which is exactly the gap
   umbrella 4 closes.
