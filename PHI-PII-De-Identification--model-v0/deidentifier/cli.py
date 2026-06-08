"""
Command-line interface for the de-identification pipeline.

Usage
-----
Default engine (regex + spaCy — existing workflow):
    python -m deidentifier path/to/file.txt
    deidentify path/to/file.txt

Presidio engine (Microsoft Presidio — new workflow):
    python -m deidentifier path/to/file.txt --engine presidio
    deidentify path/to/file.txt --engine presidio

Both engines:
    accept the same flags (--policy, --output, --audit, --strategy, etc.)
    return the same DeidentificationResult
    write audit logs in the same JSONL format
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .audit import AuditLogger
from .config import PolicyConfig
from .engine import DeidentificationEngine, DeidentificationResult
from .entities import Strategy


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deidentify",
        description=(
            "De-identify PHI/PII in a text file.\n\n"
            "Engine choices:\n"
            "  default  — built-in regex + spaCy (no extra dependencies)\n"
            "  presidio — Microsoft Presidio (requires: pip install presidio-analyzer presidio-anonymizer)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "file",
        metavar="FILE",
        help="Path to the input text file to de-identify.",
    )
    parser.add_argument(
        "--engine",
        choices=["default", "presidio"],
        default="default",
        metavar="ENGINE",
        help="Detection engine: 'default' (regex + spaCy) or 'presidio'. Default: default",
    )
    parser.add_argument(
        "--policy",
        metavar="YAML",
        help="Path to a custom policy YAML file. Defaults to the built-in policy.",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Write de-identified text to FILE instead of stdout.",
    )
    parser.add_argument(
        "--audit",
        metavar="FILE",
        help="Write audit log (JSON) to FILE.",
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
        help="Minimum confidence score 0.0–1.0 (default: from policy, usually 0.7).",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Skip the spaCy NLP model; run regex-only detection. "
            "Faster startup, lower recall on PERSON/LOCATION/DATE_TIME. "
            "Applies to the default engine only."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        metavar="FORMAT",
        help="Output format: 'text' (de-identified content only) or 'json' (full result). Default: text",
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


def _run_default_engine(
    text: str,
    document_id: str,
    policy: PolicyConfig,
    audit_logger: AuditLogger,
    fast: bool,
) -> Optional[DeidentificationResult]:
    spacy_model: Optional[str] = None if fast else "en_core_web_trf"
    engine = DeidentificationEngine(
        policy=policy,
        audit_logger=audit_logger,
        spacy_model=spacy_model,
    )
    return engine.process(text, document_id=document_id)


def _run_presidio_engine(
    text: str,
    document_id: str,
    policy: PolicyConfig,
    audit_logger: AuditLogger,
) -> Optional[DeidentificationResult]:
    try:
        from .presidio_engine import PresidioEngine
    except ImportError as exc:
        print(f"Error loading Presidio engine: {exc}", file=sys.stderr)
        return None

    try:
        engine = PresidioEngine(policy=policy, audit_logger=audit_logger)
        return engine.process(text, document_id=document_id)
    except ImportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None
    except OSError as exc:
        print(
            f"Error: spaCy model not found ({exc}).\n"
            "Install with: python -m spacy download en_core_web_trf",
            file=sys.stderr,
        )
        return None


def _format_output(result: DeidentificationResult, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(
            {
                "document_id": result.document_id,
                "engine": "presidio" if hasattr(result, "_engine") else "default",
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

    # Validate input file
    input_path = Path(args.file)
    if not input_path.exists():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        return 1

    text = input_path.read_text(encoding="utf-8")
    document_id = input_path.name

    # Build shared objects
    policy = _load_policy(args)
    audit_logger = AuditLogger(log_path=args.audit)

    # Dispatch to selected engine
    if args.engine == "presidio":
        print(f"Engine: presidio | File: {input_path}", file=sys.stderr)
        result = _run_presidio_engine(text, document_id, policy, audit_logger)
    else:
        mode = "regex-only (fast)" if args.fast else "regex + spaCy"
        print(f"Engine: default ({mode}) | File: {input_path}", file=sys.stderr)
        result = _run_default_engine(text, document_id, policy, audit_logger, args.fast)

    if result is None:
        return 1

    # Write de-identified output
    output = _format_output(result, args.format)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Output written to: {args.output}", file=sys.stderr)
    else:
        print(output)

    # Write audit log
    if args.audit:
        audit_logger.export(args.audit)
        print(f"Audit log written to: {args.audit}", file=sys.stderr)

    # Summary to stderr
    print(
        f"Entities found: {result.audit_record.entities_found} | "
        f"Processed: {result.audit_record.entities_processed}",
        file=sys.stderr,
    )

    return 0
