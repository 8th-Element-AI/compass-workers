"""Pricing cache for the Cost lens.

Rates live in Postgres `components.pricing` (JSONB). They're tiny (~8 rows) and
change rarely, but are read on every cost span — so we hold them in an in-process
dict, refreshed on a TTL, with a lazy refetch when an unknown component_id shows
up (so a newly-added model is picked up without a restart).

The source is pluggable behind `PricingSource`:
  * PostgresPricingSource  - production: reads the components table
  * StaticPricingSource    - tests / offline: a fixed dict
A Redis-backed source (or a Redis pub/sub invalidation layer) can slot in later
without touching the cost logic.

Pricing JSON shapes by component_type:
  model         -> input_per_1k, output_per_1k, cached_input_per_1k
  tool          -> per_call
  knowledgebase -> per_query
  skill/function/memory -> {} (no direct monetary cost)
"""
from __future__ import annotations
import json
import time


class PricingSource:
    def load(self) -> dict:
        """Return {component_id: {"component_type": str, "pricing": {...}}}."""
        raise NotImplementedError


class StaticPricingSource(PricingSource):
    def __init__(self, data: dict):
        self._data = dict(data)

    def load(self) -> dict:
        return dict(self._data)


class PostgresPricingSource(PricingSource):
    def __init__(self, dsn: str):
        self.dsn = dsn

    def load(self) -> dict:
        import psycopg
        out = {}
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT component_id, component_type::text, COALESCE(pricing, '{}'::jsonb) "
                "FROM components WHERE is_active = true"
            )
            for cid, ctype, pricing in cur.fetchall():
                if not isinstance(pricing, dict):
                    pricing = json.loads(pricing)
                out[cid] = {"component_type": ctype, "pricing": pricing or {}}
        return out


class PricingCache:
    def __init__(self, source: PricingSource, ttl: float = 300.0):
        self.source = source
        self.ttl = ttl
        self._data: dict = {}
        self._loaded_at = 0.0

    def _refresh(self, force: bool = False):
        now = time.time()
        if force or not self._data or (now - self._loaded_at) > self.ttl:
            try:
                self._data = self.source.load()
                self._loaded_at = now
            except Exception:
                if not self._data:      # never loaded -> surface the error
                    raise
                # otherwise keep serving the last good snapshot

    def get(self, component_id: str):
        # Serve entirely from the in-memory snapshot. The TTL refresh reloads the
        # whole component set periodically, which is what picks up a newly-added
        # component — so a miss returns None instantly and never hits Postgres.
        self._refresh()
        return self._data.get(component_id)

    def rates(self, component_id: str) -> dict:
        rec = self.get(component_id)
        return (rec or {}).get("pricing", {}) or {}
