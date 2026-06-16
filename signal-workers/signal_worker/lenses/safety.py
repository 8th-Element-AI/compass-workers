"""Safety lens — PII + toxicity (prompt injection, moderation).

Decomposed into four independently gated analysis steps:

  pii              — Presidio over input + output  -> pii_count, pii_detected
  toxicity_router  — FastText over input + output  -> routing decisions
  prompt_injection — DeBERTa PI over input         -> prompt_injection_detected
  moderation       — DeBERTa Mod over input+output -> toxicity_detected

Per-batch flow:

  process_batch(spans)
    -> Stage 1 gate (drop spans with no active safety toggle)
    -> for each step:
         spans_needing = sub-filter by step.metrics
         if empty: skip step entirely (its model NEVER loads)
         else: extract texts, dedup, step.analyze() in one batched call, cache
    -> for each kept span: build_context reads all 4 caches -> compute -> emit

Each step's model loads only the first time it's actually called with non-empty
input. If your PG thresholds only enable `pii_count`, the toxicity_router
step's `_spans_needing()` returns [] and FastText / both DeBERTas stay on
disk. Same idea for `prompt_injection_detected` vs `toxicity_detected` —
toggle one, the other model is untouched.
"""
from __future__ import annotations
 
import logging
import time
 
from ..base import parse_meta
from ..spec import MetricSpec, PrefillStep, SpecWorker
from ..utils import LRUCache
from ..patterns import ctx_value
from ..predicates import llm_call
 
log = logging.getLogger("signal.worker.safety")

LENS = "safety"

# These sets drive the toggle-gate sub-filtering (_spans_needing(kept, step.metrics)).
# They MUST match metric names in SPECS exactly. Adding a metric requires updating
# both the relevant set here and the SPECS list below.
PII_METRICS              = {"pii_count", "pii_detected"}
PROMPT_INJECTION_METRICS = {"prompt_injection_detected"}
MODERATION_METRICS       = {"toxicity_detected"}
TOXICITY_ALL_METRICS     = PROMPT_INJECTION_METRICS | MODERATION_METRICS

def _spec(metric, applies, pattern, inputs, unit, window="1h",
          threshold=False, per_span=True, meta_fn=None):
    return MetricSpec(
        metric=metric, lens=LENS, applies=applies, pattern=pattern,
        inputs=inputs, unit=unit, window=window, threshold=threshold,
        per_span=per_span, meta_fn=meta_fn,
    )

# ---- meta_fn: attach per-row detail to metric_meta -----------------------------

def _pii_meta(span, ctx):
    if not ctx.get("pii_types"):
        return None
    meta = {"types": ctx["pii_types"]}
    if ctx.get("pii_location"):
        meta["location"] = ctx["pii_location"]
    return meta

def _prompt_injection_meta(span, ctx):
    score = float(ctx.get("prompt_injection_score", 0.0))
    if score == 0.0 and not ctx.get("prompt_injection_ran"):
        return None
    meta = {
        "prompt_injection_score": round(score, 4),
        "triggered_models": ctx.get("triggered_models", []),
    }
    if ctx.get("router_ft_scores"):
        meta["routing"] = {
            "fasttext": ctx["router_ft_scores"],
            "rule_reasons": ctx.get("router_rule_reasons", []),
        }
    return meta

def _toxicity_meta(span, ctx):
    harmful = float(ctx.get("harmful_content_score", 0.0))
    sexual  = float(ctx.get("sexual_content_score", 0.0))
    if harmful == 0.0 and sexual == 0.0 and not ctx.get("moderation_ran"):
        return None
    meta = {
        "harmful_content_score": round(harmful, 4),
        "sexual_content_score":  round(sexual, 4),
        "triggered_models": ctx.get("triggered_models", []),
    }
    if ctx.get("moderation_location"):
        meta["moderation_location"] = ctx["moderation_location"]
    if ctx.get("router_ft_scores"):
        meta["routing"] = {
            "fasttext": ctx["router_ft_scores"],
            "rule_reasons": ctx.get("router_rule_reasons", []),
        }
    return meta


SPECS = [
    # PII
    _spec("pii_count",    llm_call, ctx_value("pii_count"), ["metadata.input + metadata.output -> presidio.entity_count"], unit="count", window="1h", threshold=True, meta_fn=_pii_meta),
    _spec("pii_detected", llm_call, ctx_value("pii_detected"), ["pii_count > 0"], unit="ratio", window="1h", threshold=True),

    # Prompt injection (input only)
    _spec("prompt_injection_detected", llm_call, ctx_value("prompt_injection_detected"), ["metadata.input -> fasttext + prompt_injection BERT"], unit="ratio", window="1h", threshold=True, meta_fn=_prompt_injection_meta),

    # Toxicity (harmful + sexual via moderation BERT, OR-aggregated, input + output)
    _spec("toxicity_detected", llm_call, ctx_value("toxicity_detected"), ["metadata.input + metadata.output -> fasttext + moderation BERT"], unit="ratio", window="1h", threshold=True, meta_fn=_toxicity_meta),
]

# ---- text extractors ----------------------------------------------------------

def _extract_input_and_output(span):
    """PII, toxicity_router, and moderation all use this.

    The router needs the union of what downstream steps want (input + output)
    so PI (input only) and Mod (input + output) both find routing data when
    they look up the router cache by text hash.
    """
    md = parse_meta(span.get("metadata"))
    return (md.get("input"), md.get("output"))

def _extract_input_only(span):
    """prompt_injection only — model output isn't a prompt-injection attack."""
    md = parse_meta(span.get("metadata"))
    return (md.get("input"),)


class SafetyWorker(SpecWorker):
    """Safety lens — PII + toxicity, four lazy-loaded analysis steps.

    See module docstring for the per-batch flow. The short story: each step
    runs only if at least one span in the batch has a matching active
    toggle, and the underlying model is constructed only on first call to
    that step's analyze() — so a worker that never sees an active
    `toxicity_detected` toggle never instantiates the moderation BERT.
    """
    lens = LENS
    specs = SPECS
    span_types = ("model_call",)

    def __init__(self, cfg, toggle_cache=None):
        super().__init__(cfg, toggle_cache=toggle_cache)

        # PII config
        self.pii_batch_size = cfg.signal_pii_batch_size
        self.ner_model = cfg.signal_pii_ner_model

        self.toxicity_batch_size = cfg.signal_toxicity_batch_size

        # Lazy holders. _tox is just a config + lazy model registry — its own
        # internal .fasttext/.prompt_injection/.moderation properties load
        # the actual weights on first access.
        self._pii_engine = None
        self._tox = None

        # Result caches — one per analyzer.
        self._pii_cache    = LRUCache(cfg.signal_pii_cache_max)
        self._router_cache = LRUCache(cfg.signal_toxicity_cache_max)
        self._pi_cache     = LRUCache(cfg.signal_toxicity_cache_max)
        self._mod_cache    = LRUCache(cfg.signal_toxicity_cache_max)

        # Step registry — the shared engine iterates these IN ORDER.
        # toxicity_router MUST run before prompt_injection and moderation so
        # they can read its routing decisions from the cache.
        self._steps = [
            PrefillStep(
                name="pii",
                metrics=PII_METRICS,
                cache=self._pii_cache,
                extract=_extract_input_and_output,
                analyze=self._analyze_pii,
            ),
            PrefillStep(
                name="toxicity_router",
                metrics=TOXICITY_ALL_METRICS,
                cache=self._router_cache,
                extract=_extract_input_and_output,
                analyze=self._analyze_router,
            ),
            PrefillStep(
                name="prompt_injection",
                metrics=PROMPT_INJECTION_METRICS,
                cache=self._pi_cache,
                extract=_extract_input_only,
                analyze=self._analyze_prompt_injection,
            ),
            PrefillStep(
                name="moderation",
                metrics=MODERATION_METRICS,
                cache=self._mod_cache,
                extract=_extract_input_and_output,
                analyze=self._analyze_moderation,
            ),
        ]
        
    # ------------------------------------------------------------------
    # Lazy holders
    # ------------------------------------------------------------------
    @property
    def pii_engine(self):
        if self._pii_engine is None:
            from deidentifier import PresidioEngine
            log.info("[safety] loading PresidioEngine (ner_model=%s)", self.ner_model)
            self._pii_engine = PresidioEngine.get_instance(ner_model=self.ner_model)
        return self._pii_engine

    @property
    def tox(self):
        """ToxicityClassifier: config holder + lazy model registry. Constructing
        it touches no model weights; accessing .fasttext / .prompt_injection /
        .moderation is what loads them.

        Config comes from pydantic Settings (env-driven), not runtime.yaml.
        """
        if self._tox is None:
            from toxicity_observability import ToxicityClassifier
            cfg_dict = self._build_toxicity_config()
            log.info(
                "[safety] loading ToxicityClassifier (device=%s, models_root=%s)",
                cfg_dict["runtime"]["device"],
                self.cfg.signal_toxicity_models_root,
            )
            self._tox = ToxicityClassifier(config_dict=cfg_dict)
        return self._tox

    def _build_toxicity_config(self):
        """Project pydantic Settings into the dict shape ToxicityClassifier expects.

        Model paths: absolute paths pass through, relative ones are joined onto
        `signal_toxicity_models_root`. This way one env var (the root) parks the
        whole model tree in a container, and the four individual path fields
        rarely need to be touched.
        """
        from pathlib import Path
        c = self.cfg
        root = Path(c.signal_toxicity_models_root)

        def _resolve(rel: str) -> str:
            p = Path(rel)
            return str(p if p.is_absolute() else root / p)

        return {
            "models": {
                "fasttext_router":            {"local_path": _resolve(c.signal_toxicity_fasttext_path)},
                "prompt_injection":           {"local_path": _resolve(c.signal_toxicity_pi_path)},
                "prompt_injection_onnx_int8": {"local_path": _resolve(c.signal_toxicity_pi_onnx_path)},
                "moderation":                 {"local_path": _resolve(c.signal_toxicity_mod_path)},
            },
            "thresholds": {
                "attack_route":            c.signal_toxicity_attack_route,
                "moderation_route":        c.signal_toxicity_moderation_route,
                "fast_allow":              c.signal_toxicity_fast_allow,
                "fasttext_direct":         c.signal_toxicity_fasttext_direct,
                "prompt_injection_review": c.signal_toxicity_pi_threshold,
                "harmful_content_review":  c.signal_toxicity_harmful_threshold,
                "sexual_review":           c.signal_toxicity_sexual_threshold,
            },
            "runtime": {
                "device":            c.signal_toxicity_device,
                "max_length":        c.signal_toxicity_max_length,
                "fp16_on_cuda":      c.signal_toxicity_fp16,
                "full_scan_default": False,
            },
        }

    # ------------------------------------------------------------------
    # Step analyzers — the `analyze` callables on each PrefillStep.
    # The engine passes texts that are unique and not yet in THAT step's cache.
    # ------------------------------------------------------------------

    def _analyze_pii(self, texts):
        log.info("[safety:pii] analyzing %d unique texts", len(texts))
        return self.pii_engine.analyze_batch(
            texts, batch_size=self.pii_batch_size,
        )
 
    def _analyze_router(self, texts):
        """FastText routing + deterministic rules, batched. One cache entry
        per text holds the decision PI/Mod will consult.
        """
        from toxicity_observability.normalize import normalize
        from toxicity_observability.deterministic import ATTACK, MODERATION, evaluate as deterministic_gate

        log.info("[safety:router] FastText over %d unique texts", len(texts))

        c = self.cfg
        ft_direct = c.signal_toxicity_fasttext_direct

        norms = [normalize(t) for t in texts]
        rules = [deterministic_gate(n) for n in norms]
        ft_results = self.tox.fasttext.predict_batch([n.detection_text for n in norms])

        out = []
        for ft, rule, norm in zip(ft_results, rules, norms):
            ft_scores = ft["scores"]
            run_attack = (
                ATTACK in rule.force_route
                or ft_scores["attack"] >= c.signal_toxicity_attack_route
            )
            run_moderation = (
                MODERATION in rule.force_route
                or ft_scores["moderation"] >= c.signal_toxicity_moderation_route
            )
            fast_allow = (
                rule.allow_fast_skip
                and ft_scores["attack"] < c.signal_toxicity_fast_allow
                and ft_scores["moderation"] < c.signal_toxicity_fast_allow
            )
            out.append({
                "ft_scores": ft_scores,
                "model_text": norm.model_text,
                "run_attack": run_attack,
                "run_moderation": run_moderation,
                "fast_allow": fast_allow,
                "ft_direct_attack":     ft_scores["attack"]     >= ft_direct,
                "ft_direct_moderation": ft_scores["moderation"] >= ft_direct,
                "rule_reasons": rule.reasons,
            })
        return out

    def _analyze_prompt_injection(self, texts):
        """PI BERT — only on texts FastText routed to attack and that didn't
        fast-allow. The model itself is loaded lazily on first call to
        self.tox.prompt_injection.classify_batch().
        """
        out = [None] * len(texts)
        bert_indices, bert_texts = [], []

        for i, t in enumerate(texts):
            r = self._router_cache.get(self._hash(t))
            if r is None:
                # Shouldn't happen — router extracts a superset of what PI sees.
                # Guard so we don't crash on edge cases.
                out[i] = {"score": 0.0, "ran": False, "reason": "no_router"}
                continue
            if r["ft_direct_attack"]:
                # FastText was confident enough; use its score, skip BERT.
                out[i] = {"score": float(r["ft_scores"]["attack"]), "ran": False, "reason": "ft_direct"}
            elif r["run_attack"] and not r["fast_allow"]:
                bert_indices.append(i)
                bert_texts.append(r["model_text"])
            else:
                out[i] = {"score": 0.0, "ran": False, "reason": "not_routed"}

        if bert_texts:
            log.info("[safety:pi] PI BERT over %d of %d routed texts",
                     len(bert_texts), len(texts))
            results = self.tox.prompt_injection.classify_batch(
                bert_texts, batch_size=self.toxicity_batch_size,
            )
            for idx, res in zip(bert_indices, results):
                out[idx] = {
                    "score": float(res["scores"]["prompt_injection"]),
                    "ran": True,
                    "reason": "routed",
                }
        else:
            log.info("[safety:pi] FastText cleared all %d texts; PI BERT not loaded",
                     len(texts))
        return out

    def _analyze_moderation(self, texts):
        """Moderation BERT — only on texts FastText routed to moderation and
        that didn't fast-allow. Lazy load on first classify_batch call.
        """
        out = [None] * len(texts)
        bert_indices, bert_texts = [], []

        for i, t in enumerate(texts):
            r = self._router_cache.get(self._hash(t))
            if r is None:
                out[i] = {"harmful_score": 0.0, "sexual_score": 0.0, "ran": False, "reason": "no_router"}
                continue
            if r["ft_direct_moderation"]:
                out[i] = {
                    "harmful_score": float(r["ft_scores"]["moderation"]),
                    "sexual_score":  0.0,
                    "ran": False, "reason": "ft_direct",
                }
            elif r["run_moderation"] and not r["fast_allow"]:
                bert_indices.append(i)
                bert_texts.append(r["model_text"])
            else:
                out[i] = {"harmful_score": 0.0, "sexual_score": 0.0, "ran": False, "reason": "not_routed"}

        if bert_texts:
            log.info("[safety:mod] Moderation BERT over %d of %d routed texts",
                     len(bert_texts), len(texts))
            results = self.tox.moderation.classify_batch(
                bert_texts, batch_size=self.toxicity_batch_size,
            )
            for idx, res in zip(bert_indices, results):
                out[idx] = {
                    "harmful_score": float(res["scores"]["harmful_content"]),
                    "sexual_score":  float(res["scores"]["sexual"]),
                    "ran": True,
                    "reason": "routed",
                }
        else:
            log.info("[safety:mod] FastText cleared all %d texts; Moderation BERT not loaded",
                     len(texts))
        return out

    # ------------------------------------------------------------------
    # process_batch: gate -> loop over steps -> shared engine
    # ------------------------------------------------------------------
    def process_batch(self, spans):
        t_start = time.time()
        original_count = len(spans)

        # Stage 1 — drop spans with no active safety threshold
        kept = self.filter_spans_by_gate(spans)
        skipped_at_gate = original_count - len(kept)
        if skipped_at_gate:
            log.info("[safety] %d/%d spans skipped at gate before any analysis",
                     skipped_at_gate, original_count)
        if not kept:
            return []

        # Run each registered prefill — generic, time it uniformly.
        # _prefill_cache returns 0 (and logs) when no spans need that step, so
        # the model stays unloaded.
        step_timings = []
        for step in self._steps:
            t = time.time()
            n_texts = self._prefill_cache(kept, step)
            step_timings.append(
                (step.name, round((time.time() - t) * 1000, 1), n_texts)
            )

        # Stage 2 (in compute) + emit, via shared engine
        t_emit = time.time()
        rows = self._process_kept(original_count, kept, skipped_at_gate)
        emit_ms = round((time.time() - t_emit) * 1000, 1)

        total_ms = round((time.time() - t_start) * 1000, 1)
        step_str = " | ".join(f"{n}={ms}ms ({k} texts)" for n, ms, k in step_timings)

        log.info(
            "[safety] latency | total=%.1fms | %s | emit=%.1fms | rows=%d",
            total_ms, step_str, emit_ms, len(rows),
        )
        return rows

    # ------------------------------------------------------------------
    # build_context — read all four caches, summarize per span
    # ------------------------------------------------------------------
    def build_context(self, span):
        md = parse_meta(span.get("metadata"))
        input_text  = md.get("input")  or ""
        output_text = md.get("output") or ""

        # ---- PII (input + output) ----
        in_pii  = self._pii_cache.get(self._hash(input_text))  if input_text  else None
        out_pii = self._pii_cache.get(self._hash(output_text)) if output_text else None

        in_count  = in_pii.entity_count  if in_pii  else 0
        out_count = out_pii.entity_count if out_pii else 0
        total_pii = in_count + out_count

        types = {}
        for res in (in_pii, out_pii):
            if res:
                for t, c in res.entities.items():
                    types[t] = types.get(t, 0) + c

        if in_count and out_count:
            pii_location = "both"
        elif in_count:
            pii_location = "input"
        elif out_count:
            pii_location = "output"
        else:
            pii_location = None

        # ---- Router (just for meta — read by input text, the most common case) ----
        router_in = self._router_cache.get(self._hash(input_text)) if input_text else None

        # ---- Prompt injection (input only) ----
        pi_in = self._pi_cache.get(self._hash(input_text)) if input_text else None
        pi_score = float(pi_in["score"]) if pi_in else 0.0
        pi_ran   = bool(pi_in and pi_in.get("ran"))

        # ---- Moderation (input AND output, max per dimension) ----
        mod_in  = self._mod_cache.get(self._hash(input_text))  if input_text  else None
        mod_out = self._mod_cache.get(self._hash(output_text)) if output_text else None

        harmful_in  = float(mod_in["harmful_score"])  if mod_in  else 0.0
        harmful_out = float(mod_out["harmful_score"]) if mod_out else 0.0
        sexual_in   = float(mod_in["sexual_score"])   if mod_in  else 0.0
        sexual_out  = float(mod_out["sexual_score"])  if mod_out else 0.0

        harmful_score = max(harmful_in, harmful_out)
        sexual_score  = max(sexual_in,  sexual_out)

        mod_ran = bool((mod_in and mod_in.get("ran")) or (mod_out and mod_out.get("ran")))

        side_in  = harmful_in  > 0.0 or sexual_in  > 0.0
        side_out = harmful_out > 0.0 or sexual_out > 0.0
        if side_in and side_out:
            mod_location = "both"
        elif side_in:
            mod_location = "input"
        elif side_out:
            mod_location = "output"
        else:
            mod_location = None

        # ---- Thresholds (lazy — touches self.tox only if any toxicity metric ran) ----
        # If no toxicity step ran for this span, none of the toxicity ctx fields
        # are read and self.tox is never touched. But pi_score / harmful_score
        # are already 0.0 in that case, so toxicity_detected/pi_detected default
        # to 0.0 below without needing the config.
        if router_in is not None or pi_ran or mod_ran:
            pi_threshold      = self.cfg.signal_toxicity_pi_threshold
            harmful_threshold = self.cfg.signal_toxicity_harmful_threshold
            sexual_threshold  = self.cfg.signal_toxicity_sexual_threshold
        else:
            pi_threshold = harmful_threshold = sexual_threshold = 0.50  # any default works; all scores are 0.0

        # ---- Decisions ----
        pi_detected  = 1.0 if pi_score      >= pi_threshold       else 0.0
        tox_detected = 1.0 if (
            harmful_score >= harmful_threshold or sexual_score >= sexual_threshold
        ) else 0.0

        # ---- Triggered models (for meta) ----
        triggered = []
        if router_in is not None:
            triggered.append("fasttext_router")
        if pi_ran:
            triggered.append("prompt_injection")
        if mod_ran:
            triggered.append("moderation")

        return {
            # PII
            "pii_count":     float(total_pii),
            "pii_detected":  1.0 if total_pii > 0 else 0.0,
            "pii_types":     sorted(types.keys()),
            "pii_location":  pii_location,
            # Prompt injection
            "prompt_injection_detected": pi_detected,
            "prompt_injection_score":    pi_score,
            "prompt_injection_ran":      pi_ran,
            # Toxicity (moderation)
            "toxicity_detected":     tox_detected,
            "harmful_content_score": harmful_score,
            "sexual_content_score":  sexual_score,
            "moderation_location":   mod_location,
            "moderation_ran":        mod_ran,
            # Router (for meta)
            "router_ft_scores":    (router_in["ft_scores"]     if router_in else None),
            "router_rule_reasons": (router_in.get("rule_reasons") if router_in else None),
            "triggered_models": triggered,
        }
