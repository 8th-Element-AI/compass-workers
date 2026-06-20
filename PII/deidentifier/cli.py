"""
Command-line interface for PII detection.

Usage:
    python -m deidentifier path/to/file.txt
    python -m deidentifier path/to/file.txt --format json
    python -m deidentifier path/to/file.txt --output report.json --format json
    python -m deidentifier path/to/file.txt --score-threshold 0.5
    python -m deidentifier path/to/file.txt --ner-model gravitee-io/bert-small-pii-detection
    python -m deidentifier path/to/file.txt --evaluate
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .config import PolicyConfig
from .result import AnalysisResult, EvaluationResult


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deidentify",
        description="Detect PHI/PII in a text file using Presidio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("file", metavar="FILE", help="Input text file to scan.")
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
        help="Write the detection report to FILE instead of stdout.",
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
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help=(
            "Also score entity combinations for re-identification risk "
            "(adds a 'violations' section to the report)."
        ),
    )
    return parser


def _load_policy(args: argparse.Namespace) -> PolicyConfig:
    policy = PolicyConfig.from_yaml(args.policy) if args.policy else PolicyConfig.default()
    if args.score_threshold is not None:
        policy.score_threshold = args.score_threshold
    return policy


def _format_output(
    result: AnalysisResult,
    fmt: str,
    document_id: str,
    evaluation: Optional[EvaluationResult] = None,
) -> str:
    if fmt == "json":
        payload = {
            "document_id": document_id,
            "has_pii": result.has_pii,
            "entity_count": result.entity_count,
            "entities": result.entities,
        }
        if evaluation is not None:
            payload["violations"] = [
                {
                    "kind": v.kind.value,
                    "severity": v.severity.value,
                    "rule_name": v.rule_name,
                    "entity_types": [e.entity_type for e in v.entities],
                    "span": list(v.span),
                    "score": round(v.score, 4),
                }
                for v in evaluation.violations
            ]
            payload["max_severity"] = evaluation.max_severity.value
        return json.dumps(payload, indent=2)

    lines = [
        f"Document: {document_id}",
        f"PII detected: {'yes' if result.has_pii else 'no'}",
        f"Total entities: {result.entity_count}",
    ]
    for entity_type, count in sorted(result.entities.items()):
        lines.append(f"  {entity_type}: {count}")
    if evaluation is not None:
        lines.append(f"Violations: {len(evaluation.violations)} (max severity: {evaluation.max_severity.value})")
        for v in evaluation.violations:
            entity_types = ", ".join(e.entity_type for e in v.entities)
            lines.append(f"  [{v.severity.value.upper()}] {v.rule_name} ({entity_types}) @ {v.span}")
    return "\n".join(lines)


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

    try:
        from .presidio.engine import PresidioEngine
        engine = PresidioEngine(
            ner_model=args.ner_model,
            policy=policy,
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

    result = engine.analyze(text)
    evaluation = engine.evaluate(text) if args.evaluate else None

    output = _format_output(result, args.format, document_id, evaluation)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Output written to: {args.output}", file=sys.stderr)
    else:
        print(output)

    summary = f"PII detected: {result.has_pii} | Entities found: {result.entity_count}"
    if evaluation is not None:
        summary += f" | Violations: {len(evaluation.violations)} (max severity: {evaluation.max_severity.value})"
    print(summary, file=sys.stderr)
    return 0
