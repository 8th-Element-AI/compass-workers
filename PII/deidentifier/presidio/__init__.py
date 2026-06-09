from .engine import PresidioEngine

import warnings

# spacy-huggingface-pipelines warns once per misaligned/skipped annotation.
# These are typically low-confidence model outputs on structured text (JSON,
# code) that wouldn't pass our score threshold anyway. The bridge is correctly
# refusing to use them — we just don't want one warning per document.
warnings.filterwarnings(
    "ignore",
    message="Skipping annotation.*",
    category=UserWarning,
)
# Presidio's analyzer also warns once per unmapped entity label per document.
# The label map now covers the gravitee labels we hit; this catches anything else.
warnings.filterwarnings(
    "ignore",
    message="Entity .* is not mapped to a Presidio entity.*",
)

__all__ = ["PresidioEngine"]
