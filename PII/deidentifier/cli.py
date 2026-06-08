"""
Command-line interface for de-identification.

Usage:
    python -m deidentifier path/to/file.txt
    python -m deidentifier path/to/file.txt --format json
    python -m deidentifier path/to/file.txt --output clean.txt --audit audit.jsonl
    python -m deidentifier path/to/file.txt --strategy mask
    python -m deidentifier path/to/file.txt --score-threshold 0.5
    python -m deidentifier path/to/file.txt --ner-model gravitee-io/bert-small-pii-detection
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import AuditLogger
from .config import PolicyConfig
from .entities import Strategy
from .result import DeidentificationResult


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deidentify",
        description="De-identify PHI/PII in a text file using Presidio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("file", metavar="FILE", help="Input text file to de-identify.")
    parser.add_argument(
        "--ner-model",
        metavar="MODEL",
        default=None,
        help=(
            "NER model for entity detection. "
            "Pass a HuggingFace model ID (e.g. gravitee-io/bert-small-pii-detection) "
            "or a spaCy model name (e.g. en_core_web_lg). "
            "Default: None (regex + Presidio pattern recognizers only, fastest)."
        ),
    )
    parser.add_argument(
        "--policy",
        metavar="YAML",
        help="Path to a custom policy YAML file.",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Write de-identified text to FILE instead of stdout.",
    )
    parser.add_argument(
        "--audit",
        metavar="FILE",
        help="Write audit log (JSONL) to FILE.",
    )
    parser.add_argument(
        "--strategy",
        choices=["redact", "mask", "replace"],
        metavar="STRATEGY",
        help="Override de-identification strategy for all entity types.",
    )
    parser.add_argument(
        "--score-threshold",
        type=float,
        default=None,
        metavar="FLOAT",
        help="Minimum confidence score 0.0–1.0 (default: from policy).",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        metavar="FORMAT",
        help="Output format: 'text' or 'json'. Default: text.",
    )
    return parser


def _load_policy(args: argparse.Namespace) -> PolicyConfig:
    policy = PolicyConfig.from_yaml(args.policy) if args.policy else PolicyConfig.default()
    if args.score_threshold is not None:
        policy.score_threshold = args.score_threshold
    if args.strategy:
        override = Strategy(args.strategy)
        policy.default_strategy = override
        for entity_cfg in policy.entities.values():
            entity_cfg.strategy = override
    return policy


def _format_output(result: DeidentificationResult, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(
            {
                "document_id": result.document_id,
                "deidentified_text": result.deidentified_text,
                "entities_found": result.audit_record.entities_found,
                "entities_processed": result.audit_record.entities_processed,
                "entries": [e.to_dict() for e in result.audit_record.entries],
            },
            indent=2,
        )
    return result.deidentified_text


def run(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    input_path = Path(args.file)
    if not input_path.exists():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        return 1

    text = input_path.read_text(encoding="utf-8")
    document_id = input_path.name
    policy = _load_policy(args)
    audit_logger = AuditLogger(log_path=args.audit)

    try:
        from .presidio.engine import PresidioEngine
        engine = PresidioEngine(
            ner_model=args.ner_model,
            policy=policy,
            audit_logger=audit_logger,
        )
    except ImportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"Error: spaCy model not found ({exc}).\n"
            f"Install with: python -m spacy download {args.ner_model or 'en_core_web_sm'}",
            file=sys.stderr,
        )
        return 1

    backend = f"transformers:{args.ner_model}" if args.ner_model else "regex-only"
    print(f"Engine: presidio ({backend}) | File: {input_path}", file=sys.stderr)

    result = engine.process(text, document_id=document_id)

    output = _format_output(result, args.format)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Output written to: {args.output}", file=sys.stderr)
    else:
        print(output)

    if args.audit:
        audit_logger.export(args.audit)
        print(f"Audit log written to: {args.audit}", file=sys.stderr)

    print(
        f"Entities found: {result.audit_record.entities_found} | "
        f"Processed: {result.audit_record.entities_processed}",
        file=sys.stderr,
    )
    return 0
