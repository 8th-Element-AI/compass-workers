"""
Pluggable token/text extractors for LLM responses.

Built-in extractors for OpenAI, Anthropic, and Gemini are registered by
default.  Users can register custom extractors that take priority.
"""

from obs_sdk.extractors.registry import ExtractorRegistry, TokenExtractor

__all__ = ["ExtractorRegistry", "TokenExtractor"]

# Auto-register built-in extractors
from obs_sdk.extractors import openai as _openai  # noqa: F401
from obs_sdk.extractors import anthropic as _anthropic  # noqa: F401
from obs_sdk.extractors import gemini as _gemini  # noqa: F401
