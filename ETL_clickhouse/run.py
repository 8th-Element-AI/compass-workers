#!/usr/bin/env python3
"""Entry point for the ETL worker.

Pulls spans from otel_traces (OTel Collector default table) and writes
formatted rows into compass_raw_spans so all Compass lens workers can
consume them as normal.

Usage:
    # Run continuously (production):
    python -m ETL_clickhouse.run

    # Process one batch and exit (smoke-test / CI):
    python -m ETL_clickhouse.run --once
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys

from .config import Config
from .worker import ETLWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ETL worker: otel_traces → compass_raw_spans"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one batch then exit (useful for testing).",
    )
    return parser.parse_args()


def main() -> int:
    args   = parse_args()
    cfg    = Config()
    worker = ETLWorker(cfg)

    def _on_signal(_sig, _frm):
        worker.stop()

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)

    try:
        worker.run_poll(once=args.once)
    except KeyboardInterrupt:
        worker.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
