"""Text preprocessing helpers.

Used by `pipeline.LocalScorer` at runtime AND by `eval/benchmark_*.py` —
the eval scripts deliberately call the same helpers so the benchmark
measures what the lens actually computes, not a re-implementation.

JSON-shaped outputs are flattened into 'key is value.' sentences before
sentence-level scoring because NLI models are trained on natural
language, not raw JSON tokens.
"""
from __future__ import annotations

import json
import re

from .constants import MAX_SENTS, SENT_MIN_CHARS

# Split on terminal punctuation followed by whitespace, or on hard newlines.
# Lookbehind so the punctuation stays attached to the preceding sentence.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def split_sentences(
    text: str,
    max_sents: int = MAX_SENTS,
    sent_min_chars: int = SENT_MIN_CHARS,
) -> list[str]:
    """Split text into clean sentence list, capped at `max_sents`.

    Empty / near-empty fragments (< sent_min_chars) are dropped so they don't
    pollute NLI / embedding pair counts.
    """
    if not text:
        return []
    sents = [s.strip() for s in _SENT_SPLIT.split(text)]
    return [s for s in sents if len(s) >= sent_min_chars][:max_sents]


def _flatten_json(obj, prefix: str = "") -> list[str]:
    """Recursively flatten a JSON object into 'key is value.' lines.

    Lists are walked element-wise (capped at 20 to bound runaway payloads);
    nested dicts use dot-separated key paths.
    """
    lines: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                lines.extend(_flatten_json(v, f"{prefix}{k}."))
            else:
                lines.append(f"{prefix}{k} is {v}.")
    elif isinstance(obj, list):
        for v in obj[:20]:
            if isinstance(v, (dict, list)):
                lines.extend(_flatten_json(v, prefix))
            else:
                lines.append(f"{prefix}{v}.")
    return lines


def normalize_output(text: str) -> str:
    """JSON-looking output -> 'key is value.' sentences; anything else unchanged.

    Detection is intentionally cheap: a leading `{` or `[`. If json.loads
    fails (malformed JSON, embedded JSON in prose, etc.) the original text
    is returned and sentence splitting runs on it as-is.
    """
    if not text:
        return ""
    t = text.strip()
    if t[:1] not in "{[":
        return text
    try:
        obj = json.loads(t)
    except Exception:
        return text
    flat = _flatten_json(obj)
    return "\n".join(flat) if flat else text