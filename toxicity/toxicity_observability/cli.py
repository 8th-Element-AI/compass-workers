from __future__ import annotations

import argparse
import json
import sys

from .download import download_models
from .pipeline import ToxicityClassifier


def main() -> None:
    parser = argparse.ArgumentParser(prog="toxicity-observe")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # download
    d = sub.add_parser("download", help="Download model artifacts from HuggingFace.")
    d.add_argument("--config", default=None)

    # classify
    c = sub.add_parser("classify", help="Run the pipeline over a text.")
    c.add_argument("text", nargs="*", help="Text to classify; omitted reads stdin.")
    c.add_argument("--config", default=None)
    c.add_argument("--device", choices=("cpu", "cuda"), default=None,
                   help="Device for the PyTorch prompt-injection model.")
    c.add_argument("--onnx-provider", choices=("auto", "cpu", "cuda"), default=None,
                   help="Execution provider for the MiniLM ONNX moderation model.")
    c.add_argument("--max-length", type=int, default=None)
    c.add_argument("--include-raw", action="store_true",
                   help="Include per-model raw outputs in the response.")

    args = parser.parse_args()

    if args.cmd == "download":
        print(json.dumps(download_models(args.config), indent=2))
        return

    text = " ".join(args.text).strip() or sys.stdin.read()
    clf = ToxicityClassifier(
        args.config,
        device=args.device,
        onnx_provider=args.onnx_provider,
        max_length=args.max_length,
    )
    print(json.dumps(clf.classify(text, include_raw=args.include_raw), indent=2))


if __name__ == "__main__":
    main()