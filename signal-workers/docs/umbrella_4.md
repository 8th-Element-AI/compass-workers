# Umbrella 4 — Structural Validation (Quality Lens)

> Design doc for the fourth of four Quality-lens metric umbrellas.
> Status: v1 implemented (heuristic checks) · Owner: Quality lens worker
> Companion diagram: `umbrella_4_flow.png`

---

## 1. Metrics captured

Deterministic checks of output **shape against a declared contract** — no models,
no judgment calls.

| Metric | Type | Question it answers | Failure it catches | Thresholdable |
|---|---|---|---|---|
| `schema_conformance` | boolean | Does the output match the expected schema? | Missing/extra/wrong-typed fields | ✅ |
| `format_correctness` | boolean | Does the output parse as its required format? | Truncated/malformed JSON, wrong encoding | ✅ |
| `constraint_satisfaction` | boolean | Are explicit constraints satisfied (length, range, pattern…)? | Contract violations that parse fine | — |
| `tool_call_validity` | boolean | Are tool calls well-formed (name, args, response)? | Bad tool invocations | ✅ |

Booleans average into **pass rates** at rollup (e.g. avg of schema_conformance
over 1h = conformance rate).

**Data contract (v1):**

| Span type | Fields used |
|---|---|
| `model_call` | `metadata.output` (+ optional declared `schema` / `output_format`) |
| `tool_call` | `metadata.tool`, `metadata.request`, `metadata.response`, `span_status` |
| `validation` | `metadata.valid`, `metadata.output.errors[]` |
| any | optional `metadata.constraints` (the convention, §4) |

---

## 2. Where this sits in signal-workers

```
signal_worker/lenses/quality.py
├── _format_correctness()        ⭐  capture helper
├── _schema_conformance()        ⭐  capture helper
├── _tool_call_validity()        ⭐  capture helper
├── _constraint_satisfaction()   ⭐  capture helper
└── SPECS: 4 entries (output_bearing / schema_checked / any_span / tool_op)
```

Mechanical and free, like Umbrella 3. Runs with semantic scoring disabled.

---

## 3. ⭐ Capture recipes (v1 — implemented)

| Metric | Recipe | Evidence in `metric_meta` |
|---|---|---|
| `format_correctness` | declared format checked if present; else heuristic: JSON-looking output (starts `{`/`[`) must parse; plain text has no requirement to violate → 1.0 | — |
| `schema_conformance` | `validation` spans: trust the recorded `valid` flag · `model_call`: only when a schema is declared in metadata — required keys present in parsed output | — |
| `tool_call_validity` | three checks: tool name non-empty · request parses to dict/list · response present when status is ok | `failed` (check names) |
| `constraint_satisfaction` | **convention**: `metadata.constraints = {max_chars, min_chars, max_words, min_words, contains[], not_contains[], format}` — emits only when declared; unknown keys reported as `unchecked`, never failed | `violated`, `unchecked` |

### Live-run results (real ClickHouse run, full dataset)

| Metric | Rows | Mean | Reads as |
|---|---|---|---|
| `format_correctness` | 5,261 | 1.0 | all synthetic outputs are well-formed JSON |
| `schema_conformance` | 709 | 0.9661 | exactly the 24 seeded invalid validation spans |
| `tool_call_validity` | 1,392 | 1.0 | all synthetic tool calls well-formed |
| `constraint_satisfaction` | 0 | — | correct: nothing declares constraints yet |

### v1 limitations

- `format_correctness` only catches malformed JSON — narrow by design until a
  format contract exists.
- `schema_conformance` on model_call checks **key presence only** — no types,
  enums, ranges, nesting.
- `tool_call_validity` doesn't check args against the tool's actual signature
  (needs the components registry).
- `constraint_satisfaction` emits nothing until instrumentation adopts the
  convention.
