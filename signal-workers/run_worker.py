#!/usr/bin/env python3
"""Run Signal Lens workers.

Examples:
    python run_worker.py --worker performance

    python run_worker.py --worker all

    python run_worker.py --worker performance --once

    python run_worker.py --worker performance \
        --csv ./signal_raw_spans.csv \
        --out ./perf_derived.csv
"""

import argparse
import csv
import logging
import threading

from dotenv import load_dotenv

from signal_worker.base import DER_COLS
from signal_worker.config import Config
from signal_worker.lenses.cost import CostWorker
from signal_worker.lenses.performance import PerformanceWorker
from signal_worker.lenses.safety import SafetyWorker

logger = logging.getLogger(__name__)

LENSES = {
    "performance": PerformanceWorker,
    "cost": CostWorker,
    "safety": SafetyWorker,
}


def build_worker(lens: str, cfg: Config, offline: bool):
    if lens == "cost" and offline:
        # offline (--csv): no Postgres -> use the known seed rates
        from signal_worker.pricing import PricingCache, StaticPricingSource, default_pricing
        return CostWorker(cfg, pricing=PricingCache(StaticPricingSource(default_pricing())))
    return LENSES[lens](cfg)


def run_worker( lens: str, once: bool = False, csv_path: str | None = None, out_path: str | None = None) -> None:
    worker = build_worker(lens=lens, cfg=Config.from_env(), offline=bool(csv_path))

    if csv_path:
        rows = worker.run_csv(csv_path)
        output_file = out_path or f"{lens}_derived.csv"
        with open(output_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(DER_COLS)
            for row in rows:
                writer.writerow(
                    ["\\N" if row.get(col) is None else row.get(col) for col in DER_COLS]
                )
        logger.info("%s metric rows written to %s", len(rows), output_file)
        return

    try:
        logger.info("Starting %s worker", lens)
        worker.run_poll(once=once)
    except KeyboardInterrupt:
        logger.info("%s worker received shutdown signal", lens)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Signal Lens Worker Manager")
    parser.add_argument("--worker", choices=sorted(LENSES) + ["all"], default="all", help="Worker to run (default: all)")
    parser.add_argument("--once", action="store_true", help="Process one batch and exit")
    parser.add_argument("--csv", help="Offline mode: read spans from CSV")
    parser.add_argument("--out", help="Offline mode: output CSV path")
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    args = parse_args()

    if args.csv and args.worker == "all":
        raise ValueError("--csv mode requires a specific worker, not 'all'")

    if args.worker == "all":
        logger.info("Starting all workers: %s", ", ".join(LENSES))
        threads = [
            threading.Thread(
                target=run_worker,
                kwargs={"lens": lens, "once": args.once},
                name=f"{lens}Worker",
                daemon=False,
            )
            for lens in LENSES
        ]
        for thread in threads:
            thread.start()
        try:
            for thread in threads:
                thread.join()
        except KeyboardInterrupt:
            logger.info("Received shutdown signal, workers will terminate gracefully")
            for thread in threads:
                thread.join()
        return

    run_worker(lens=args.worker, once=args.once, csv_path=args.csv, out_path=args.out)


if __name__ == "__main__":
    main()
