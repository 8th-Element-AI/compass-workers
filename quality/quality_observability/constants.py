"""Default recipe constants.

These are the defaults baked into the package. The `recipes:` block in
`configs/runtime.yaml` (or `config_dict["recipes"]` passed by signal-workers)
overrides them per-instance — change the YAML, not the code.
"""
from __future__ import annotations

# Grounding text truncation for NLI premise.
# NLI cross-encoders are quadratic in sequence length; clipping the premise
# trades a small amount of recall for predictable per-pair latency.
PREMISE_MAX_CHARS: int = 2000

# Max sentences per side after split_sentences. Caps the number of NLI
# pairs we generate from any one output: O(out_sents) for faithfulness +
# O(out_sents) for coherence + O(in_sents * out_sents) for completeness.
MAX_SENTS: int = 10

# Minimum sentence length to keep after splitting. Drops fragments like
# stray periods, single tokens, and JSON delimiters that survived
# normalize_output.
SENT_MIN_CHARS: int = 3

# Cosine threshold above which a retrieved chunk is considered "used"
# by the answer (chunk_utilization recipe). 0.5 is a defensible midpoint
# for L2-normalized MiniLM embeddings — tune via the benchmark if needed.
CHUNK_USED_COS: float = 0.5