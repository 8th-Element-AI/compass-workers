#!/usr/bin/env python3
"""Run a Signal lens worker.

Examples:
  # production-ish: poll ClickHouse continuously
  python run_worker.py performance

  # process one batch and exit
  python run_worker.py performance --once

  # offline: compute over an exported spans CSV, write a derived CSV (no ClickHouse)
  python run_worker.py performance --csv ./signal_raw_spans.csv --out ./perf_derived.csv
"""
import sys
import csv
import argparse
import logging

from signal_worker.config import Config
from signal_worker.base import DER_COLS
from signal_worker.lenses.performance import PerformanceWorker
from signal_worker.lenses.cost import CostWorker

LENSES = {
    "performance": PerformanceWorker,
    "cost": CostWorker,
    # quality, safety, outcomes -> added as each lens lands
}


def build_worker(lens, cfg, offline):
    if lens == "cost" and offline:
        # offline (--csv): no Postgres -> use the known seed rates
        from signal_worker.pricing import PricingCache, StaticPricingSource, default_pricing
        return CostWorker(cfg, pricing=PricingCache(StaticPricingSource(default_pricing())))
    return LENSES[lens](cfg)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("lens", choices=sorted(LENSES))
    ap.add_argument("--once", action="store_true", help="process one batch then exit (poll mode)")
    ap.add_argument("--csv", help="offline: read spans from this CSV instead of ClickHouse")
    ap.add_argument("--out", help="offline: write derived rows to this CSV")
    args = ap.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker = build_worker(args.lens, Config.from_env(), offline=bool(args.csv))

    if args.csv:
        rows = worker.run_csv(args.csv)
        out = args.out or f"{args.lens}_derived.csv"
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(DER_COLS)
            for r in rows:
                w.writerow(["\\N" if r.get(c) is None else r.get(c) for c in DER_COLS])
        print(f"{len(rows)} metric rows -> {out}")
        return

    worker.run_poll(once=args.once)


if __name__ == "__main__":
    main()
