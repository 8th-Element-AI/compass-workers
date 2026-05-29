#!/usr/bin/env python3
"""Print the registered metric specs for a lens.

    python show_specs.py performance
"""
import sys
from signal_worker.lenses.performance import PerformanceWorker
from signal_worker.lenses.cost import CostWorker

LENSES = {"performance": PerformanceWorker, "cost": CostWorker}


def main():
    lens = sys.argv[1] if len(sys.argv) > 1 else "performance"
    w = LENSES[lens](None)
    print(f"# {lens} lens — {len(w.specs)} specs ({sum(s.per_span for s in w.specs)} per-span, "
          f"{sum(not s.per_span for s in w.specs)} read-time)\n")
    hdr = f"{'metric':24} {'applies':14} {'pattern':22} {'unit':8} {'win':5} {'thr':3} {'per_span':8}"
    print(hdr)
    print("-" * len(hdr))
    for s in w.specs:
        pat = getattr(s.pattern, "__qualname__", "")
        pat = pat.split(".")[0] if pat else type(s.pattern).__name__
        print(f"{s.metric:24} {s.applies.__name__:14} {pat:22} {s.unit:8} {s.window:5} "
              f"{'Y' if s.threshold else '-':3} {'Y' if s.per_span else 'read':8}")


if __name__ == "__main__":
    main()
