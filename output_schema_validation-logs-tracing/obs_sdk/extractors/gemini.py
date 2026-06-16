"""Gemini response extractor for GenerateContentResponse-style responses."""

from __future__ import annotations

from typing import Any, Optional, Tuple

from obs_sdk.extractors.registry import ExtractorRegistry


def gemini_extractor(response: Any, prompt: Any) -> Optional[Tuple[int, int, str]]:
    """Extract tokens and text from a Gemini GenerateContentResponse.

    How it identifies a Gemini response:
        Checks for response.usage_metadata — only Gemini responses have this.
        If not present, returns None so the next extractor can try.

    Token fields:
        Handles both snake_case (prompt_token_count) and camelCase
        (promptTokenCount) because different Gemini SDK versions use
        different naming.

    Output text:
        Tries response.text first (shortcut), falls back to iterating
        response.candidates[0].content.parts[].text.

    Returns (input_tokens, output_tokens, output_text) or None.
    """
    if response is None:
        return None

    # This is the key check — only Gemini responses have usage_metadata
    usage_meta = getattr(response, "usage_metadata", None)
    if usage_meta is None:
        return None

    # Handle both snake_case and camelCase attribute names
    input_tokens = (
        getattr(usage_meta, "prompt_token_count", 0)
        or getattr(usage_meta, "promptTokenCount", 0)
        or 0
    )
    output_tokens = (
        getattr(usage_meta, "candidates_token_count", 0)
        or getattr(usage_meta, "candidatesTokenCount", 0)
        or 0
    )

    # Extract output text — try .text shortcut first
    output_text = ""
    try:
        text_prop = getattr(response, "text", None)
        if text_prop and isinstance(text_prop, str):
            output_text = text_prop
    except Exception:
        pass

    if not output_text:
        try:
            candidates = getattr(response, "candidates", None)
            if candidates:
                parts_obj = getattr(
                    getattr(candidates[0], "content", None), "parts", None
                )
                if parts_obj:
                    parts = []
                    for p in parts_obj:
                        t = getattr(p, "text", None)
                        if t:
                            parts.append(t)
                    if parts:
                        output_text = "".join(parts)
        except Exception:
            pass

    return input_tokens, output_tokens, output_text


# Auto-register when this module is imported.
# The registry tries extractors in LIFO order, so this gets tried
# after OpenAI and Anthropic (which are imported later).
ExtractorRegistry.register(gemini_extractor)
