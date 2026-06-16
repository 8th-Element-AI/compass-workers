#!/usr/bin/env python3
"""Run Signal Lens workers.

Single lens (the production default — one process per lens, see ARCHITECTURE.md):

    python run_worker.py --worker performance
    python run_worker.py --worker performance --once
    python run_worker.py --worker safety

All lenses in one process (dev convenience — coupled lifecycle):

    python run_worker.py --worker all
    python run_worker.py --worker all --once

Show registered specs for a lens and exit:

    python run_worker.py --worker performance --specs
"""

import argparse
import logging
import signal as _signal
import sys
import threading

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


def build_worker(lens: str, cfg: Config):
    return LENSES[lens](cfg)


def _run_single(worker, once: bool, lens: str) -> None:
    logger.info("Starting %s worker", lens)
    worker.run_poll(once=once)


def _run_all(cfg: Config, once: bool) -> int:
    """Run every lens concurrently in this process — one thread per lens.

    Lifecycle is coupled (one crash takes them all down). For production-grade
    isolation, run separate processes — see ARCHITECTURE.md S8.
    """
    workers = [build_worker(lens, cfg) for lens in LENSES]
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
    parser.add_argument("--specs", action="store_true", help="Show registered specs and exit")
    return parser.parse_args()

def show_specs(lens: str, cfg: Config) -> None:
    w = LENSES[lens](cfg)

    print(
        f"# {lens} lens — {len(w.specs)} specs "
        f"({sum(s.per_span for s in w.specs)} per-span, "
        f"{sum(not s.per_span for s in w.specs)} read-time)\n"
    )

    hdr = (
        f"{'metric':24} {'applies':14} {'pattern':22} "
        f"{'unit':8} {'win':5} {'thr':3} {'per_span':8}"
    )

    print(hdr)
    print("-" * len(hdr))

    for s in w.specs:
        pat = getattr(s.pattern, "__qualname__", "")
        pat = pat.split(".")[0] if pat else type(s.pattern).__name__

        print(
            f"{s.metric:24} {s.applies.__name__:14} "
            f"{pat:22} {s.unit:8} {s.window:5} "
            f"{'Y' if s.threshold else '-':3} "
            f"{'Y' if s.per_span else 'read':8}"
        )

def main() -> int:
    args = parse_args()

    cfg = Config()

    if args.specs:
        show_specs(args.worker, cfg)
        return 0

    if args.worker == "all":
        return _run_all(cfg, args.once)

    worker = build_worker(args.worker, cfg)

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
