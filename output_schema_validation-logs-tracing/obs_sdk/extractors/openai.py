"""OpenAI response extractor for ChatCompletion-style responses."""

from __future__ import annotations

from typing import Any, Optional, Tuple

from obs_sdk.extractors.registry import ExtractorRegistry


def openai_extractor(response: Any, prompt: Any) -> Optional[Tuple[int, int, str]]:
    """Extract tokens and text from an OpenAI ChatCompletion response.

    How it identifies an OpenAI response:
        Checks for response.usage.prompt_tokens — only OpenAI uses this name.
        (Anthropic uses input_tokens instead.)

    Output text:
        Reads response.choices[0].message.content (chat completions) or
        response.choices[0].text (legacy completions).

    Returns (input_tokens, output_tokens, output_text) or None.
    """
    if response is None:
        return None

    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    prompt_tokens = getattr(usage, "prompt_tokens", None)
    if prompt_tokens is None:
        return None

    input_tokens = prompt_tokens or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0

    # Extract output text
    output_text = ""
    try:
        choices = getattr(response, "choices", None)
        if choices:
            first = choices[0]
            msg = getattr(first, "message", None)
            if msg:
                output_text = getattr(msg, "content", "") or ""
            else:
                output_text = getattr(first, "text", "") or ""
    except Exception:
        pass

    return input_tokens, output_tokens, output_text


ExtractorRegistry.register(openai_extractor)
