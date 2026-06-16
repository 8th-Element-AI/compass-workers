# Umbrella 4 — Structural Validation Flow (Explained)

> Companion to `umbrella_4_flow.png`. A demo-friendly walkthrough of how the
> Quality lens checks whether an output has the **right shape** — does it match
> its schema, parse as its format, satisfy declared constraints, and are tool
> calls well-formed. Grounded in the actual code
> (`signal_worker/lenses/quality.py`). See also the design doc `umbrella_4.md`.

## The one-sentence pitch

> "Umbrellas 1–2 ask 'is this answer any good?' Umbrella 4 asks the prerequisite
> question: 'is this output even well-formed?' A beautiful answer that's
> truncated JSON is still broken — this catches that. Purely mechanical: no
> models, no judgment, effectively free."

## The big picture (top to bottom)

Same assembly-line shape as umbrella 3 — **no orange scorer box**, because no
models run. The flow branches at the top on span type, runs deterministic checks
in `build_context`, and emits boolean rows. Booleans **average into pass rates**
at rollup (e.g. avg `schema_conformance` over an hour = "conformance rate").

---

## What umbrella 4 measures — 4 boolean checks

| Metric | Plain-English question | What it catches | Thresholdable |
|---|---|---|---|
| **`schema_conformance`** | "Does the output match the expected schema?" | Missing / extra / wrong-typed fields | ✅ |
| **`format_correctness`** | "Does the output parse as its required format?" | Truncated / malformed JSON, wrong encoding | ✅ |
| **`constraint_satisfaction`** | "Are explicit constraints met (length, range, pattern)?" | Contract violations that *parse fine* but break a rule | — |
| **`tool_call_validity`** | "Are tool calls well-formed (name, args, response)?" | Bad tool invocations | ✅ |

> Demo framing: "This is structural, not semantic. It doesn't care if the answer
> is *right* — only whether it's *shaped correctly* so downstream systems can
> consume it."

**Cost profile:** mechanical and free, like umbrella 3. Runs with semantic
scoring disabled.

---

## Why the diagram branches at `span_type?`

Each check applies to different span types, so the flow forks at the top:

- `format_correctness` → `model_call`, `tool_call`, `validation`
- `schema_conformance` → `model_call`, `validation`
- `tool_call_validity` → `tool_call` only
- `constraint_satisfaction` → any span (emits only when constraints are declared)

---

## How the checks actually work (`quality.py`)

### `format_correctness` (lines 141–154) — deliberately narrow

If a format is declared, check it. Otherwise the only enforceable rule is
"JSON-looking output (starts with `{`/`[`) must actually parse." Plain text has
no format to violate, so it scores **1.0**. By design this only catches malformed
JSON today — it's narrow until a richer format contract exists.

### `schema_conformance` (lines 157–169) — two modes

- **`validation` spans:** just trust the recorded `valid` flag.
- **`model_call` spans:** only checkable **when a schema is declared** in
  metadata — and even then it's **key-presence only** (are the required keys in
  the output?). No type / range / enum / nesting checks. No declared schema → no
  row emitted.

### `tool_call_validity` (lines 172–181) — three checks

1. Tool name is non-empty.
2. Request parses to a dict/list.
3. Response is present when the status is OK.

The names of any failed checks go into `metric_meta` for audit.

### `constraint_satisfaction` (lines 208–245) — a convention

Driven by a declared `metadata.constraints` object:
`{max_chars, min_chars, max_words, min_words, contains[], not_contains[],
format}`. Only emits when constraints are declared.

> Smart detail worth pointing out: **unknown constraint keys are reported as
> `unchecked`, never counted as failures.** So adding a new constraint type can't
> silently tank the score — it just shows up as "not yet checkable."

Both `violated` and `unchecked` lists ride along into `metric_meta`.

---

## Walking through the boxes

**1. `signal_raw_spans` (blue cylinder, top)**
The immutable ClickHouse source.

**2. `span_type?` (the diamond)**
Routes each span to whichever checks apply to it.

**3. `build_context` (per span, mechanical, no models)**
Runs the four capture helpers above and stashes their booleans plus any evidence.

**4. `4 MetricSpecs emit via ctx_value()`**
`format_correctness`, `schema_conformance`, `tool_call_validity`,
`constraint_satisfaction`. Failed-check names / violations ride along into
`metric_meta`.

**5. `signal_derived_metrics` (blue cylinder)**
One row per metric per span, bulk-inserted per batch, watermark advances.

**6. `mv_agg_base` → `signal_aggregated_metrics`**
Materialized view auto-fires on insert; booleans aggregate into pass rates per
1-minute bucket.

**7. dashboards / thresholds → signals**
Pass rates surface on dashboards; Postgres thresholds fire alerts.

---

## v1 limitations (good honesty points for the demo)

- `format_correctness` only catches malformed JSON — narrow by design until a
  format contract exists.
- `schema_conformance` on `model_call` checks **key presence only** — no types,
  enums, ranges, or nesting.
- `tool_call_validity` doesn't check args against the tool's actual signature.
- `constraint_satisfaction` emits nothing until instrumentation adopts the
  convention.

---

## Live-run sanity check (credibility point)

On the real full dataset:

| Metric | Rows | Mean | Reads as |
|---|---|---|---|
| `format_correctness` | 5,261 | 1.0 | all synthetic outputs are well-formed JSON |
| `schema_conformance` | 709 | 0.9661 | exactly the 24 seeded invalid spans |
| `tool_call_validity` | 1,392 | 1.0 | all synthetic tool calls well-formed |
| `constraint_satisfaction` | 0 | — | correct — nothing declares constraints yet |

The `constraint_satisfaction = 0 rows` is a nice teaching point: it's not broken,
it's *correctly* emitting nothing because no span has opted into the convention
yet.

---

## One-glance summary table

| Metric | Span type(s) | Recipe | Evidence |
|---|---|---|---|
| **format_correctness** | model_call · tool_call · validation | declared format if present, else JSON-looking output must parse | — |
| **schema_conformance** | model_call · validation | validation: trust `valid`; model_call: required keys present (when schema declared) | — |
| **tool_call_validity** | tool_call | name non-empty · request parses · response present when status ok | `failed` checks |
| **constraint_satisfaction** | any | declared `metadata.constraints` checked; unknown keys = unchecked | `violated`, `unchecked` |

---

## The 3 themes to hit in your demo

1. **What it measures:** structural correctness — does the output have the right
   *shape* (schema, format, constraints, tool-call form), independent of whether
   it's *good*.
2. **Why it's cheap:** purely mechanical, zero models, runs even with semantic
   scoring disabled.
3. **Where it's honest about its limits:** v1 checks are intentionally
   conservative (key-presence, malformed-JSON), and it only flags what it can
   actually verify — `constraint_satisfaction` stays silent until producers
   declare constraints, rather than guessing.
