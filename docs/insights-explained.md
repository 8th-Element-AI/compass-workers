# Insights Engine — Explained (Plain-English Walkthrough)

> This is the friendly, everything-explained version. For the terse technical spec
> see [`insights.md`](./insights.md); for diagrams see [`docs/insights/`](./insights/).
> Here we explain **every piece, in plain language, with examples** — no prior context
> needed.

Throughout, we'll follow one running example:

> **The "Triager" agent's responses are getting slow.**

---

## 1. What problem does this solve?

We collect tons of numbers about our AI system — how fast each step is, how often it
errors, how much it costs, whether outputs are valid. That's great, but **nobody can
stare at thousands of numbers all day.**

The **Insights Engine** is the thing that watches those numbers for you and says:

> "Heads up — the Triager agent's latency just crossed the danger line."

It turns raw numbers into a short list of **"here's what's wrong, ranked by how bad."**
Those alerts are called **insights**. When several insights look like they're part of
the same underlying problem, it bundles them into an **incident**.

That's the whole job: **numbers → prioritized "here's what's wrong."**

---

## 2. The two databases (the cast of characters)

Everything revolves around two storage systems. Keep these straight and the rest is easy.

### ClickHouse — the giant logbook of numbers
- Stores **measurements over time**: every step the AI took, and metrics like latency,
  cost, errors.
- It's **huge** (think billions of rows) and **append-only** — you only ever *add* to
  it, never edit. Like a logbook where you keep writing new lines and never erase.
- Built to crunch massive amounts of time-based data fast.

### Postgres — the smaller "settings + results" database
- Stores **the rules** ("latency above 3600ms is bad"), the **directory of things**
  (what "agt_triage" actually is), and — new — **the engine's output** (the insights
  and incidents).
- It's smaller and **editable** — rows get updated (an insight can go from "open" to
  "resolved").

**One-line rule:** *ClickHouse = the big pile of measurements. Postgres = the rules and
the alerts.*

---

## 3. How the data gets ready (before the engine even runs)

The engine doesn't generate any measurements itself — it relies on stuff other workers
already put in place. Here's that setup, in order.

### 3a. Spans — the raw events
Every little action the AI takes (call a model, run a tool, fetch a document) is
recorded as a **span** — one row with a start time, end time, status, and which
agent/model it belonged to. Spans land in ClickHouse.

### 3b. Lens workers — they compute the metrics
A set of background workers called **lenses** read the raw spans and compute the actual
numbers:
- **Performance** lens → latency, error rate, etc.
- **Cost** lens → token spend.
- **Safety** lens → harmful/PII checks.
- **Quality** lens → is the output valid/coherent.
- (**Outcomes** — planned.)

Each lens writes its numbers into ClickHouse as **derived metrics** (one number per span
per metric).

### 3c. Aggregation — squashing millions of rows into summaries
Here's a key idea. We don't want the engine wading through millions of individual
numbers. So ClickHouse automatically **rolls them up by the minute**.

Example — say in one minute the Triager agent had **10** calls with these latencies:

```
1200, 1500, 1800, 2000, 2200, 2600, 3100, 3400, 3800, 4100  (milliseconds)
```

Those **10 rows get collapsed into 1 summary row** for that minute:

```
minute 12:00 · count=10 · min=1200 · max=4100 · average≈2570 · p95≈4000
```

The individual 10 are still kept elsewhere, but the engine reads this **one tidy
summary row** instead of 10. This is the **aggregated metrics** table, and it's what the
engine actually looks at. (Multiply that by millions of calls and you see why this
matters — the engine reads small summaries, not the firehose.)

### 3d. The rules — what counts as "bad"
Separately, in Postgres there's a **thresholds** table — the rulebook. Each rule says
something like:

> metric = `latency`, for `agent` Triager, over a `5-minute` window:
> **warning** above 3600ms, **critical** above 7200ms.

A worker called the **reconciler** auto-discovers entities and seeds these rule rows.
By the time the engine runs, the rulebook is already there.

So before the engine starts: **ClickHouse has the minute-by-minute summaries, Postgres
has the rulebook.**

---

## 4. The engine is a worker that wakes up on a timer

The Insights Engine is its **own background program** (separate from the lenses). It
**wakes up roughly every 30–60 seconds**, does one full pass, then goes back to sleep.

### Why a timer? (and not "run right after a lens finishes")
- **The summaries only refresh once a minute.** Checking faster just re-reads the same
  numbers — nothing new to see. So ~every 30–60s is already as fresh as the data gets.
- **The lenses never "finish."** They run nonstop, every couple of seconds, and there
  are 4–5 of them. "Run after a lens finishes" would mean running constantly — you'd
  have to slow it back down to once a minute anyway.
- A timer keeps the engine **simple and independent** — it doesn't need to be wired to
  the lenses.

Think of it as a person who **glances at a scoreboard once a minute.** The scoreboard
only updates once a minute, so glancing more often is pointless.

---

## 5. What happens in one wake-up (the 6 steps)

Each time it wakes up, the engine runs **6 steps in order**. Let's walk our Triager
example through all six.

### Step 1 — SCAN ("gather the facts")
- Reads the **rulebook** from Postgres: "what am I supposed to be watching?"
- Reads the **current numbers** from ClickHouse for each rule (e.g. "Triager's average
  latency over the last 5 minutes = 4100ms").
- For "drift" rules it also reads the **recent history** (the last 7 days) to know what
  "normal" looks like.

It does **not** pull in raw spans — just the small summary numbers it needs.

### Step 2 — DETECT ("is anything actually wrong?")
Now it compares each number against its rule. Two ways it can flag trouble (explained
fully in §6):
- **Threshold:** 4100ms is above the 3600ms warning line → **flag it.**
- **Drift:** is today's number weirdly far from the usual? (not in this example)

For Triager: 4100 > 3600 → a **candidate alert** is created. Everything that's *within*
range is thrown away here.

### Step 3 — CLASSIFY ("how bad is it?")
It labels the severity:
- Above the **critical** line (7200ms) → **high**
- Above the **warning** line (3600ms) → **medium**
- 4100ms is past warning but not critical → **medium**

### Step 4 — CORRELATE ("is this part of a bigger problem?")
It checks whether this alert is related to others happening at the same time. If the
GPT-4o **model** under Triager is *also* slow right now, and the **workflow** Triager
sits in is *also* slow — those are probably the **same underlying problem**. It bundles
them into one **incident** and gives it a **confidence score** (how sure it is they're
related). More in §8.

### Step 5 — RECONCILE ("is this new, or did I already report it?")
Before saving, it checks: *did I already raise this exact alert last time?*
- **New** → create a fresh insight.
- **Already open and still bad** → just update it (new value, bump "last seen").
- **Was open but now it's fine** → mark it **resolved**.

This is how alerts **don't pile up as duplicates** and how they **clear themselves**
when the problem goes away. (How it recognizes "this exact alert" is explained in §9.)

### Step 6 — PERSIST ("write it down")
It saves the insights and incidents into Postgres. **This is the only moment in the
whole cycle that it writes anything.**

Then it sleeps until the next wake-up, where all 6 steps run again.

> Note: steps 2, 3, and 4 don't touch any database — they're just the engine **thinking**
> about the numbers it already grabbed in step 1.

---

## 6. The two ways it spots trouble (detection modes)

### Mode 1 — Threshold ("is this number past a fixed line?")
A simple comparison against a number you set. "Latency above 3600ms = bad."
- Good for things with a clear acceptable limit (latency, error rate, cost).
- Severity comes from **how far past** the line you are (warning vs critical).
- Like a **speed limit**: 70 in a 50 zone is bad; 90 is worse.

### Mode 2 — Baseline drift ("is this weirdly different from normal?")
Sometimes there's no fixed "bad" number — what matters is a **sudden change**. Drift
compares **right now** against **what's been normal lately** (the last **7 days**).
- If error rate is usually 0.5% and today it's 4%, that's a big jump — flag it, even if
  4% isn't "officially" over a hard limit.
- Like noticing your **electricity bill tripled** this month — no fixed "bad" number,
  but tripling is clearly off.

> **The "normal" window is fixed at 7 days for now.** We could later let each rule pick
> its own lookback, but 7 days is the simple default for version 1.

> *(We considered a third mode, "gate failure," but dropped it — the error-rate and
> quality checks already cover those cases.)*

---

## 7. Severity — how alerts are ranked

Every insight is **high / medium / low**. This is just so the most important problems
float to the top of the list instead of everything looking equally urgent.
- **High** — crossed the critical line (or drifted a lot). Look now.
- **Medium** — crossed the warning line.
- **Low** — minor.

---

## 8. Incidents & confidence — grouping related alerts

Imagine the model is slow. That one root cause makes **three** things look bad at once:
the model is slow, the agent using it is slow, and the workflow containing the agent is
slow. Without grouping, you'd get **three separate alerts** for **one** problem.

CORRELATE bundles them into a single **incident** so you see *"one problem, here are its
symptoms"* instead of three confusing alerts.

How does it decide they belong together? Three clues:
1. **Same time** — they're all happening right now.
2. **Same chain** — the model sits *inside* the agent, which sits *inside* the workflow
   (a parent-child chain). Related things share that chain.
3. **Moving together** — they got worse at the same time.

It blends those into a **confidence score from 0 to 1** — basically "how sure am I these
are the same problem." It also guesses the **root** (likely the model, the deepest one).

---

## 9. How an insight lives and dies (its lifecycle)

An insight has just two states:
- **open** — the problem is currently happening.
- **resolved** — it cleared up.

Every wake-up, the engine re-checks. As long as Triager is still slow, the **same**
insight stays open and just gets updated (latest value, "last seen" time). The moment
Triager's latency drops back under the line, the engine flips it to **resolved** on its
own. **You never have to manually close it.**

How does it know "this is the same alert I saw last time" and not a duplicate? It builds
a little **fingerprint** for each alert from its ingredients:

```
performance + threshold + Triager agent + latency + 5-minute window
```

Same fingerprint = same alert → update it. New fingerprint = new alert → create it. There
can only ever be **one open insight per fingerprint**, so duplicates are impossible.

This is the answer to *"do insights update when the metrics change?"* — **yes,
automatically, every wake-up.**

---

## 10. The optional AI helper (the LLM / Claude layer)

Everything above is plain math and rules. On top of that, we can **optionally** ask an
AI model (Claude) to make the alerts more **human-friendly**:
- **Recommendation** — "Triager latency is up; consider checking the model's load or
  reducing the prompt size."
- **Root-cause story** — a sentence explaining the likely cause for an incident.
- **Plain-English summary** — a readable one-liner for the incident.

Important guardrails (because AI calls are slow and cost money):
- It's only used on **high-severity** alerts and **incidents** — never on every little
  number.
- Results are **cached** (don't re-ask the same thing) and run **in the background** so
  they never slow down the main detection.
- The engine **works perfectly fine without it** — this layer only adds polish.

---

## 11. What actually gets saved

Just **two tables** in Postgres:

### `insights` — one row per active alert
Holds: what lens, which detection mode (threshold/drift), severity, status
(open/resolved), which thing it's about (Triager, etc.), the metric, the observed value,
the line it crossed, when first/last seen, and (if the AI ran) a recommendation.

### `incidents` — one row per group of related alerts
Holds: the confidence score, overall severity, the suspected root, how many alerts are
in it, and (if the AI ran) a plain-English summary.

> **Not saved:** the readable names ("Triager") and the raw spans behind the alert. Those
> are looked up only when someone opens the screen (see §12).

---

## 12. What a screen would show (when we build a UI)

> There's **no UI yet** — that's deliberately later. But here's what it would show, to
> make the storage choices make sense.

- **A list of alerts** — severity badge, what it's about, "4100ms > 3600ms", status —
  filterable by lens, severity, thing, and time.
- **Alert detail** — the numbers, the recommendation, and **evidence** (the actual slow
  calls).
- **Incident view** — the grouped alerts with the summary and confidence.

To show readable names ("Triager" instead of "agt_triage") and the evidence (the actual
slow calls), the screen does a quick **look-up at view time** — names from the Postgres
directory, evidence from ClickHouse. That's why the engine itself doesn't bother storing
them.

---

## 13. Where the data flows (who reads/writes what)

Simple rule: **the engine reads from both databases but only writes to Postgres. It
never writes to ClickHouse.**

| When | It reads… | From | It writes… | To |
|---|---|---|---|---|
| Step 1 SCAN | the rulebook | Postgres | — | — |
| Step 1 SCAN | current numbers + 7-day history | ClickHouse | — | — |
| Steps 2–4 | *(nothing — just thinking)* | — | — | — |
| Step 5 RECONCILE | the currently-open alerts | Postgres | — | — |
| Step 6 PERSIST | — | — | insights + incidents | Postgres |

So per wake-up: **3 reads, 1 write.** The big data (spans) stays in ClickHouse the whole
time.

---

## 14. Will it run out of memory on huge data? (No — here's why)

Common worry: "if there's tons of data, won't holding it in memory blow up?"

The trick: **the huge data never comes into the engine.** ClickHouse already squashed
millions of spans into per-minute summaries (§3c), so the engine pulls back **one small
number per rule**, not the millions behind it. And after Step 2, it keeps only the
**handful that are actually broken**.

So memory scales with **how many things are currently wrong** (usually a few dozen), not
with how many events happened (billions). Even 100,000 rules would be a few megabytes —
trivial.

If we ever had a truly enormous number of rules, two easy fixes (no redesign):
1. Let ClickHouse do the comparison and **only send back the breaches**.
2. Process **one solution at a time** instead of all at once.

---

## 15. Edge case: a step that started but never ended

What if a span has a start time but no end time (the operation crashed or is still
running)?
- In our system a span is **one row with both times**, written when it finishes — so an
  unfinished step usually just **isn't there yet**. The engine simply doesn't see it
  until it completes. No problem.
- If a broken row *did* sneak in with a missing end time, the latency math would produce
  garbage. The fix is **upstream**: don't compute a latency for it (skip it), and let
  the failure show up as an **error** instead. The engine downstream just trusts clean
  numbers.

(Our spans always arrive with both start and end, so this is handled.)

---

## 16. What we're building now vs later

**Building now (v1):**
- The timer-based worker.
- Two detection modes: threshold + drift (7-day "normal").
- Severity ranking.
- Grouping into incidents with a confidence score.
- Self-updating/self-resolving alerts (no duplicates).
- Saving insights + incidents to Postgres.
- The optional AI helper.

**On purpose, saved for later (each can be added cleanly without rework):**
- **No UI/API yet** — just the engine and its saved output.
- **No triage** (claim / snooze / mute / mark-false-alarm) — that's pointless without a
  UI for people to click, so it waits until there's a screen.
- **No per-rule "normal" window** — fixed at 7 days for now.

---

## 17. Glossary (quick reference)

| Term | Plain meaning |
|---|---|
| **Span** | One recorded action the AI took (one row, with start/end/status). |
| **Metric** | A measured number (latency, error rate, cost…). |
| **Lens** | A worker that computes one family of metrics (performance, cost, safety, quality). |
| **ClickHouse** | The big logbook of measurements over time (append-only). |
| **Postgres** | The smaller database for rules + the engine's alerts (editable). |
| **Aggregation** | Squashing many per-call numbers into one per-minute summary. |
| **Threshold** | A fixed "this number is bad above/below X" rule. |
| **Drift** | "This is weirdly different from the last 7 days," even without a fixed limit. |
| **Insight** | One alert: "this metric, on this thing, is bad." |
| **Incident** | A bundle of related insights that look like one underlying problem. |
| **Severity** | How urgent: high / medium / low. |
| **Confidence** | 0–1 score for how sure we are an incident's alerts belong together. |
| **Fingerprint** | The identity of an alert, used to avoid duplicates. |
| **Reconcile** | Each cycle: decide new / still-open / resolved, so alerts self-update. |
| **Tick / wake-up** | One full 6-step pass of the engine (every ~30–60s). |

---

That's the whole engine: **wake up every ~30–60s → grab the latest summarized numbers
and the rulebook → spot what's over the line or drifting → rank it → group related ones
into incidents → update/resolve without duplicates → save to Postgres.** Optionally, an
AI helper makes the alerts read like a human wrote them. The heavy data stays in
ClickHouse; the engine only ever handles small summaries.
