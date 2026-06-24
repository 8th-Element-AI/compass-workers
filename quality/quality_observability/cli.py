"""`quality-score` CLI.

Subcommands:
  download   — fetch every model in runtime.yaml from HuggingFace
  generation — score one (input, output) pair, print JSON
  retrieval  — score one (query, chunks, answer?) triple, print JSON

The generation and retrieval subcommands take a single job from CLI args
or stdin; for batched scoring use the Python API directly. This is the
quick-loop "did the recipe do what I expected" tool for development —
the production batched path runs inside the Quality lens.
"""
from __future__ import annotations

import argparse
import json
import sys

from .download import download_models
from .pipeline import LocalScorer


def _read_stdin_jobs() -> list[dict]:
    """Read JSON lines / a JSON array from stdin. Each entry is a job dict."""
    raw = sys.stdin.read().strip()
    if not raw:
        return []
    # Try array first, then JSONL.
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _cmd_download(args) -> None:
    print(json.dumps(download_models(args.config), indent=2))


def _cmd_generation(args) -> None:
    scorer = LocalScorer(
        config_path=args.config, device=args.device, batch_size=args.batch_size,
    )
    if args.input is not None or args.output is not None:
        jobs = [(args.input or "", args.output or "")]
    else:
        jobs = [(j.get("input", ""), j.get("output", "")) for j in _read_stdin_jobs()]
    print(json.dumps(scorer.score_generation(jobs), indent=2))


def _cmd_retrieval(args) -> None:
    scorer = LocalScorer(
        config_path=args.config, device=args.device, batch_size=args.batch_size,
    )
    if args.query is not None or args.chunks is not None:
        chunks = json.loads(args.chunks) if args.chunks else []
        jobs = [(args.query or "", chunks, args.answer)]
    else:
        jobs = [
            (j.get("query", ""), j.get("chunks", []), j.get("answer"))
            for j in _read_stdin_jobs()
        ]
    print(json.dumps(scorer.score_retrieval(jobs), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="quality-score")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # download
    d = sub.add_parser("download", help="Download model artifacts from HuggingFace.")
    d.add_argument("--config", default=None)
    d.set_defaults(func=_cmd_download)

    # generation
    g = sub.add_parser("generation", help="Score (input, output) generation quality.")
    g.add_argument("--input", default=None, help="Grounding context / prompt.")
    g.add_argument("--output", default=None, help="Generated text to score.")
    g.add_argument("--config", default=None)
    g.add_argument("--device", choices=("cpu", "cuda"), default=None)
    g.add_argument("--batch-size", type=int, default=None)
    g.set_defaults(func=_cmd_generation)

    # retrieval
    r = sub.add_parser("retrieval", help="Score (query, chunks, answer?) retrieval quality.")
    r.add_argument("--query", default=None)
    r.add_argument("--chunks", default=None, help="JSON array of chunk strings.")
    r.add_argument("--answer", default=None,
                   help="Optional same-trace model answer for chunk_utilization.")
    r.add_argument("--config", default=None)
    r.add_argument("--device", choices=("cpu", "cuda"), default=None)
    r.add_argument("--batch-size", type=int, default=None)
    r.set_defaults(func=_cmd_retrieval)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()