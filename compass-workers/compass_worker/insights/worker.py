"""Insights Engine worker — interval-driven evaluation loop.

Unlike the lens workers (which stream new spans past a watermark), the engine
evaluates the *current state* of metrics against the rulebook on a timer. Each
tick runs the six stages and is fully idempotent — durability lives in the
reconciled `insights` table, not a checkpoint. See docs/insights.md.

  SCAN      load rules (PG) + read current value & 7d baseline (CH)
  DETECT    threshold + drift  → candidate signals
  CLASSIFY  severity (done inside detect)
  CORRELATE group candidates into incidents
  RECONCILE diff vs open insights
  PERSIST   write insights + incidents (PG)

Reads from ClickHouse + Postgres; writes only to Postgres.
"""
from __future__ import annotations

import logging
import time

import psycopg
from prometheus_client import Counter, Gauge, Histogram

from ..base import BaseWorker
from ..observability import set_ready
from .rules import load_active_rules
from .reader import read_current, read_baseline
from .detect import evaluate_threshold, evaluate_drift
from .correlate import correlate
from .store import persist

log = logging.getLogger("compass.insights")

# ── Prometheus metrics (module-level; registered once) ───────────────
TICK_DURATION = Histogram(
    "compass_insights_tick_duration_seconds",
    "Wall-clock time per evaluation tick.",
    buckets=(0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60),
)
TICK_ERRORS = Counter(
    "compass_insights_tick_errors_total",
    "Ticks that raised before completing.",
)
OPENED_TOTAL = Counter(
    "compass_insights_opened_total", "Insights newly opened.")
RESOLVED_TOTAL = Counter(
    "compass_insights_resolved_total", "Insights auto-resolved.")
OPEN_GAUGE = Gauge(
    "compass_insights_open", "Currently-open insights.")
INCIDENTS_OPEN_GAUGE = Gauge(
    "compass_insights_incidents_open", "Currently-open incidents.")
CANDIDATES_GAUGE = Gauge(
    "compass_insights_candidates", "Candidate signals detected in the last tick.")


class InsightsWorker(BaseWorker):
    lens = "insights"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._pg = None

    # ---- PG handle (autocommit OFF — persist() owns the transaction) ----
    def _pg_conn(self):
        if self._pg is None or self._pg.closed:
            self._pg = psycopg.connect(self.cfg.pg_dsn, autocommit=False)
            log.info("[insights] PG connected")
        return self._pg

    def _open_counts(self, pg) -> tuple[int, int]:
        with pg.cursor() as cur:
            cur.execute("SELECT count(*) FROM insights WHERE status='open'")
            ins = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM incidents WHERE status='open'")
            inc = cur.fetchone()[0]
        pg.rollback()  # close the read-only transaction
        return ins, inc

    # ---- one evaluation tick (the 6 stages) ----
    def tick(self):
        cfg = self.cfg
        pg = self._pg_conn()

        # SCAN (Postgres half) — load the rulebook, then end the read txn.
        rules = load_active_rules(pg)
        pg.rollback()

        ch = self.ch()
        candidates = []
        for rule in rules:
            try:
                table = cfg.insights_agg_table
                current = read_current(ch, rule, table)   # SCAN (ClickHouse)
                candidates += evaluate_threshold(rule, current)   # DETECT + CLASSIFY
                if cfg.insights_drift_enabled:
                    baseline = read_baseline(ch, rule, cfg.insights_baseline_days, table)
                    candidates += evaluate_drift(
                        rule, current, baseline,
                        cfg.insights_drift_cutoff, cfg.insights_drift_min_samples,
                    )
            except Exception:
                log.exception("[insights] rule eval failed: %s/%s/%s",
                              rule.lens, rule.metric, rule.scope)
                continue

        incidents = correlate(candidates)                 # CORRELATE
        stats = persist(pg, candidates, incidents)        # RECONCILE + PERSIST

        OPENED_TOTAL.inc(stats.opened)
        RESOLVED_TOTAL.inc(stats.resolved)
        CANDIDATES_GAUGE.set(len(candidates))
        open_ins, open_inc = self._open_counts(pg)
        OPEN_GAUGE.set(open_ins)
        INCIDENTS_OPEN_GAUGE.set(open_inc)

        log.info(
            "[insights] tick: rules=%d candidates=%d opened=%d updated=%d "
            "resolved=%d incidents(open=%d,resolved=%d) open_total=%d",
            len(rules), len(candidates), stats.opened, stats.updated,
            stats.resolved, stats.incidents_open, stats.incidents_resolved, open_ins,
        )
        return stats

    # ---- interval loop (replaces BaseWorker's span-fetch run_poll) ----
    def run_poll(self, once: bool = False):
        tick_sec = self.cfg.insights_tick_sec
        log.info("[insights] starting eval loop (tick=%.1fs, drift=%s, baseline=%dd)",
                 tick_sec, self.cfg.insights_drift_enabled,
                 self.cfg.insights_baseline_days)
        print(f"[insights] starting eval loop (tick={tick_sec}s)")
        set_ready(True)

        while not self._stop_event.is_set():
            t0 = time.monotonic()
            try:
                self.tick()
                TICK_DURATION.observe(time.monotonic() - t0)
            except Exception:
                TICK_ERRORS.inc()
                log.exception("[insights] tick failed; will retry next interval")

            if once:
                break
            self._stop_event.wait(tick_sec)

        log.info("[insights] eval loop exited")
