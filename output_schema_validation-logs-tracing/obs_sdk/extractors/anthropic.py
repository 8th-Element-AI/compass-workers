"""Anthropic response extractor for Message-style responses."""

from __future__ import annotations

from typing import Any, Optional, Tuple

from obs_sdk.extractors.registry import ExtractorRegistry


def anthropic_extractor(response: Any, prompt: Any) -> Optional[Tuple[int, int, str]]:
    """Extract tokens and text from an Anthropic Message response.

    How it identifies an Anthropic response:
        Checks for response.usage.input_tokens AND confirms that
        response.usage.prompt_tokens does NOT exist (to avoid matching
        OpenAI responses, which also have a .usage attribute).

    Output text:
        Iterates response.content[] blocks and joins their .text fields.
        Anthropic returns content as a list of blocks (text, tool_use, etc).

    Returns (input_tokens, output_tokens, output_text) or None.
    """
    if response is None:
        return None

    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    # Anthropic uses input_tokens (not prompt_tokens)
    input_tokens = getattr(usage, "input_tokens", None)
    if input_tokens is None:
        return None

    # Make sure this isn't an OpenAI response (which also has usage)
    if hasattr(usage, "prompt_tokens"):
        return None

    output_tokens = getattr(usage, "output_tokens", 0) or 0

    # Extract output text from content blocks
    output_text = ""
    try:
        content = getattr(response, "content", None)
        if content and isinstance(content, list):
            parts = []
            for block in content:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
            output_text = " ".join(parts)
    except Exception:
        pass

    return input_tokens, output_tokens, output_text


ExtractorRegistry.register(anthropic_extractor)
