#!/usr/bin/env python3
"""Run Signal Lens workers.

Single lens (the production default — one process per lens, see ARCHITECTURE.md):

    python run_worker.py --worker performance
    python run_worker.py --worker performance --once
    python run_worker.py --worker safety

All lenses in one process (dev convenience — coupled lifecycle):

    python run_worker.py --worker all
    python run_worker.py --worker all --once

Offline (no DB):

    python run_worker.py --worker performance --csv ./signal_raw_spans.csv --out ./perf.csv
"""

import argparse
import csv
import logging
import signal as _signal
import sys
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
    "cost":        CostWorker,
    "safety":      SafetyWorker,
}


def build_worker(lens: str, cfg: Config, offline: bool):
    if lens == "cost" and offline:
        from signal_worker.pricing import PricingCache, StaticPricingSource, default_pricing
        return CostWorker(cfg, pricing=PricingCache(StaticPricingSource(default_pricing())))
    return LENSES[lens](cfg)


def _run_csv(worker, csv_path: str, out_path: str, lens: str) -> None:
    rows = worker.run_csv(csv_path)
    output_file = out_path or f"{lens}_derived.csv"
    with open(output_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(DER_COLS)
        for row in rows:
            writer.writerow(["\\N" if row.get(c) is None else row.get(c) for c in DER_COLS])
    logger.info("%s metric rows written to %s", len(rows), output_file)


def _run_single(worker, once: bool, lens: str) -> None:
    logger.info("Starting %s worker", lens)
    worker.run_poll(once=once)


def _run_all(cfg: Config, once: bool) -> int:
    """Run every lens concurrently in this process — one thread per lens.

    Lifecycle is coupled (one crash takes them all down). For production-grade
    isolation, run separate processes — see ARCHITECTURE.md §8.
    """
    workers = [build_worker(lens, cfg, offline=False) for lens in LENSES]
    threads = [
        threading.Thread(
            target=_run_single,
            args=(w, once, w.lens),
            name=f"{w.lens}Worker",
            daemon=False,
        )
        for w in workers
    ]

    def _on_signal(_sig, _frm):
        logger.info("Shutdown signal received; asking workers to stop...")
        for w in workers:
            w.stop()

    _signal.signal(_signal.SIGINT, _on_signal)
    if hasattr(_signal, "SIGTERM"):
        _signal.signal(_signal.SIGTERM, _on_signal)

    for t in threads:
        t.start()
    try:
        # Poll-join the threads so KeyboardInterrupt can land in the main thread
        # while threads are still running (a plain t.join() would block signals).
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.5)
    except KeyboardInterrupt:
        _on_signal(None, None)

    # Final join to surface any thread exceptions.
    for t in threads:
        t.join()
    logger.info("All workers stopped.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Signal Lens Worker Manager")
    parser.add_argument(
        "--worker",
        choices=sorted(LENSES) + ["all"],
        required=True,
        help="Lens to run, or 'all' for every lens in this process (dev only).",
    )
    parser.add_argument("--once", action="store_true", help="Process one batch and exit")
    parser.add_argument("--csv", help="Offline mode: read spans from CSV")
    parser.add_argument("--out", help="Offline mode: output CSV path")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    args = parse_args()

    if args.csv and args.worker == "all":
        logger.error("--csv requires a specific worker, not 'all'")
        return 2

    cfg = Config()

    if args.worker == "all":
        return _run_all(cfg, args.once)

    worker = build_worker(args.worker, cfg, offline=bool(args.csv))
    if args.csv:
        _run_csv(worker, args.csv, args.out, args.worker)
        return 0

    # Single lens: still wire Ctrl+C so the worker exits cleanly.
    def _on_signal(_sig, _frm):
        worker.stop()

    _signal.signal(_signal.SIGINT, _on_signal)
    if hasattr(_signal, "SIGTERM"):
        _signal.signal(_signal.SIGTERM, _on_signal)

    try:
        _run_single(worker, args.once, args.worker)
    except KeyboardInterrupt:
        worker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
