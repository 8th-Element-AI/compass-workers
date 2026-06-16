"""
ExtractorRegistry — pluggable token/text extraction from LLM responses.

Each extractor is a callable that receives ``(response, prompt)`` and
returns ``(input_tokens, output_tokens, output_text)`` or ``None`` if it
cannot handle the response shape.  The registry tries each extractor in
order; first non-None result wins.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Tuple, runtime_checkable, Protocol


@runtime_checkable
class TokenExtractor(Protocol):
    """Protocol for token extractors."""

    def __call__(
        self, response: Any, prompt: Any
    ) -> Optional[Tuple[int, int, str]]:
        """
        Extract token counts and output text from an LLM response.

        Returns
        -------
        tuple[int, int, str] | None
            ``(input_tokens, output_tokens, output_text)`` if this
            extractor handles the response shape, or ``None`` to pass
            to the next extractor.
        """
        ...


class ExtractorRegistry:
    """Registry of token extractors, tried in LIFO order.

    When obs.llm() gets a response back from the LLM, it calls
    ExtractorRegistry.extract(response) to figure out how many tokens
    were used and what the output text was.

    Built-in extractors (Gemini, OpenAI, Anthropic) are auto-registered
    at import time.  User-registered extractors are tried first (LIFO),
    so they can override built-ins for custom providers.
    """

    _extractors: List[Callable] = []

    @classmethod
    def register(cls, extractor: Callable) -> None:
        """
        Register an extractor.  It will be tried *before* any previously
        registered extractors.
        """
        cls._extractors.insert(0, extractor)

    @classmethod
    def extract(
        cls, response: Any, prompt: Any
    ) -> Tuple[int, int, str]:
        """
        Try each registered extractor in order.

        Returns the first non-None result, or ``(0, 0, str(response))``
        as a fallback.
        """
        for extractor in cls._extractors:
            try:
                result = extractor(response, prompt)
                if result is not None:
                    return result
            except Exception:
                continue

        # Fallback
        try:
            text = str(response) if response else ""
            return 0, 0, text
        except Exception:
            return 0, 0, ""
