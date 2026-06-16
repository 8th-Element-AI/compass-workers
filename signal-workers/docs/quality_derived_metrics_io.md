# Quality Lens — Input / Output variables for `signal_derived_metrics`

> What the Quality lens **reads** (input variables, per metric, from the raw span)
> and what it **writes** (output variables = the `signal_derived_metrics` row).
> Source of truth: `signal_worker/lenses/quality.py`, `signal_worker/scorers.py`,
> `signal_worker/spec.py` (`_row`), `signal_worker/base.py` (`DER_COLS`).

---

## A. Output: the `signal_derived_metrics` row (every quality metric writes this)

The lens emits **one row per metric per span**. The table has **18 columns** —
note there is **no `unit` / `window` / `threshold` column** (those live in the
spec registry, not in ClickHouse). All columns except `metric`, `value`, and
`metric_meta` are **span-derived and identical in structure across every quality
metric**:

| Column | CH type | Filled from | Notes |
|---|---|---|---|
| `span_id` | String | `span.span_id` | the span scored |
| `trace_id` | String | `span.trace_id` | denormalized for per-trace queries |
| `parent_span_id` | String | `span.parent_span_id` | trace tree |
| `scope` | LowCardinality(String) | `scopes_for(span)` | **`component`** for all quality spans |
| `solution_id` | String | span / `path_cols` | full entity path kept at component scope |
| `endpoint` | String | span / `path_cols` | |
| `workflow_id` | String | span / `path_cols` | |
| `agent_id` | String | span / `path_cols` | |
| `component_id` | String | span / `path_cols` | the model/tool/kb leaf |
| `component_type` | LowCardinality(String) | span / `path_cols` | model / tool / knowledgebase / … |
| `environment` | LowCardinality(String) | `span.environment` | |
| `ts` | DateTime64(3,'UTC') | `span.ended_at` | rollup timestamp (MV buckets on this) |
| **`metric`** | LowCardinality(String) | `spec.metric` | **per-metric** — the metric name (see §B) |
| **`value`** | Float64 | `pattern(span, ctx)` | **per-metric** — the score (see §B) |
| `confidence` | Nullable(Float32) | — | **always NULL today** (see note) |
| **`metric_meta`** | Nullable(String) | `meta_fn(span, ctx)` | **per-metric** — JSON audit blob or NULL (see §B) |
| `start_ts` | DateTime64(3,'UTC') | `span.started_at` | |
| `end_ts` | DateTime64(3,'UTC') | `span.ended_at` | |

Column order for INSERT (matches `DER_COLS`):

```
span_id, trace_id, parent_span_id, scope, solution_id, endpoint, workflow_id,
agent_id, component_id, component_type, environment, ts, metric, value,
confidence, metric_meta, start_ts, end_ts
```

> **confidence is unused (NULL) today.** It's the natural slot for a scorer's
> self-confidence on the semantic metrics (e.g. NLI margin, reranker score
> spread). If you want it populated, that's a small `_row`/spec change — flagging
> it since you're wiring the table.

---

## B. Per-metric: input variables + the value / metric_meta written

`metric`, `value`, and `metric_meta` are the only columns that differ by metric.
"Input variables" = the raw-span fields each metric reads (via `build_context`).
A metric emits **no row** when its inputs are absent (e.g. no schema declared, no
constraints declared) — it never writes a fabricated value.

### Umbrella 1 — output scoring (span_type = `model_call`)

| `metric` | Input variables (read) | `value` (Float64) | `metric_meta` (JSON) |
|---|---|---|---|
| `faithfulness` | `metadata.input` (premise), `metadata.output` (hypotheses) | mean NLI entailment, 0–1 | `{"out_sents":N,"min_entail":x}` |
| `coherence` | `metadata.output` | 1 − mean adjacent-sentence contradiction, 0–1 | NULL |
| `completeness` | `metadata.input` + `metadata.output` | embedding coverage of input by output, 0–1 | NULL |

### Umbrella 2 — retrieval quality (span_type = `retrieval`)

| `metric` | Input variables (read) | `value` (Float64) | `metric_meta` (JSON) |
|---|---|---|---|
| `context_relevance` | `metadata.query`, `metadata.chunks[].text` | mean per-chunk relevance, 0–1 | `{"chunks":N,"rel":[…],"used":N}` |
| `chunk_utilization` | `metadata.chunks[].text` + same-trace `model_call` `metadata.output` | used/total chunks, 0–1 | NULL |

### Umbrella 3 — data quality (span_type = `validation`, `skill_exec`)

| `metric` | Input variables (read) | `value` (Float64) | `metric_meta` (JSON) |
|---|---|---|---|
| `data_completeness` | `validation`: `metadata.input` record fields · `skill_exec`: `metadata.records_processed`, `metadata.batch_size` | non-null field fraction / records ratio, 0–1 | NULL |
| `data_accuracy` | `validation`: `metadata.output.errors[]`, `metadata.input` field count | 1 − errors/fields, 0–1 | `{"errors":[…]}` (≤20) when errors present |

### Umbrella 4 — structural validation (booleans; avg = pass rate at rollup)

| `metric` | span_type | Input variables (read) | `value` (Float64) | `metric_meta` (JSON) |
|---|---|---|---|---|
| `schema_conformance` | validation, model_call | `validation`: `metadata.valid` · `model_call`: declared schema (`expected_schema`/`response_schema`/`schema`) keys vs `metadata.output` | 1.0 / 0.0 | NULL |
| `format_correctness` | model_call, tool_call, validation | `metadata.output` (or `metadata.response` for tool_call), `metadata.output_format`/`metadata.format` | 1.0 / 0.0 | NULL |
| `constraint_satisfaction` | any | `metadata.constraints` + `metadata.output` (**emits only when constraints declared**) | 1.0 / 0.0 | `{"violated":[…],"unchecked":[…]}` |
| `tool_call_validity` | tool_call | `metadata.tool`, `metadata.request`, `metadata.response`, `span.span_status` | 1.0 / 0.0 | `{"failed":[…]}` when checks fail |

> Booleans are stored as `1.0`/`0.0` in the Float64 `value` column; a rollup
> `avg(value)` then reads as a pass rate.

---

## C. Example rows

```text
# faithfulness on a model_call span
span_id='sp-00000002' trace_id='tr-726600539' parent_span_id='' scope='component'
solution_id='sol_docprocess' endpoint='' workflow_id='wf_docunderstand'
agent_id='agt_classify' component_id='model_gpt4o' component_type='model'
environment='prod' ts='2026-05-23 04:24:01.020' metric='faithfulness'
value=0.0795 confidence=NULL metric_meta='{"out_sents":2,"min_entail":0.0074}'
start_ts='2026-05-23 04:24:00.533' end_ts='2026-05-23 04:24:01.020'

# tool_call_validity on a tool_call span (well-formed)
... scope='component' component_id='tool_websearch' component_type='tool'
metric='tool_call_validity' value=1.0 confidence=NULL metric_meta=NULL ...
```

The lens does a single bulk `INSERT` of all rows in a batch (via
`clickhouse-connect`, `column_names=DER_COLS`), then the `mv_agg_base`
materialized view rolls them into `signal_aggregated_metrics` automatically — no
extra write needed for aggregation.

---

## D. Quick reference — which span types each metric reads

| span_type | quality metrics emitted |
|---|---|
| `model_call` | faithfulness, coherence, completeness, schema_conformance, format_correctness, constraint_satisfaction* |
| `retrieval` | context_relevance, chunk_utilization |
| `tool_call` | tool_call_validity, format_correctness, constraint_satisfaction* |
| `validation` | data_completeness, data_accuracy, schema_conformance, format_correctness, constraint_satisfaction* |
| `skill_exec` | data_completeness, constraint_satisfaction* |

\* `constraint_satisfaction` applies to any span but only emits when
`metadata.constraints` is present (none in current data).
