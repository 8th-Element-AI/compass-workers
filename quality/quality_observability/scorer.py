"""QualityScorer interface + StaticScorer (test double).

Both kept torch-free on purpose: the Quality lens depends only on the
interface, so the offline `--csv` path can inject StaticScorer and run
the whole worker without sentence-transformers / torch installed.

`LocalScorer` (in pipeline.py) is the production implementation. New
backends — an LLM judge, a remote scoring service, a sampler — implement
the same two methods and slot into the lens unchanged.
"""
from __future__ import annotations


class QualityScorer:
    """Interface. Both methods take a batch of jobs and return one dict per job.

    Why batches: the recipes need many model forward passes per span
    (one NLI pair per output sentence, etc.). Batching across spans lets
    a single tokenize + model.predict() amortize call overhead across
    everything the worker pulled this iteration.
    """

    def score_generation(self, jobs: list) -> list:
        """Score generation quality.

        Args:
            jobs: list of (input_text, output_text) tuples.

        Returns:
            One dict per job:
                {"faithfulness": float | None,
                 "coherence":    float | None,
                 "completeness": float | None,
                 "meta": dict}
            Any field can be None if the underlying recipe couldn't be
            computed (no input, single-sentence output, etc.) — the lens
            treats None as "nothing to emit".
        """
        raise NotImplementedError

    def score_retrieval(self, jobs: list) -> list:
        """Score retrieval quality.

        Args:
            jobs: list of (query, [chunk_text], answer_text|None) tuples.
                `answer_text` is the same-trace model_call output if
                discoverable in the current batch; None if not (lens fills
                it best-effort).

        Returns:
            One dict per job:
                {"context_relevance": float | None,
                 "chunk_utilization": float | None,
                 "meta": dict}
        """
        raise NotImplementedError


class StaticScorer(QualityScorer):
    """Fixed mid-range scores — used by offline tests / CSV mode without torch.

    `chunk_utilization` is None when no answer was provided, matching the
    production LocalScorer's behavior (you can't measure utilization
    without an answer).
    """

    def __init__(self, value: float = 0.75):
        self.value = float(value)

    def score_generation(self, jobs):
        return [
            {
                "faithfulness": self.value,
                "coherence":    self.value,
                "completeness": self.value,
                "meta":         {"scorer": "static"},
            }
            for _ in jobs
        ]

    def score_retrieval(self, jobs):
        return [
            {
                "context_relevance": self.value,
                "chunk_utilization": self.value if ans else None,
                "meta":              {"scorer": "static"},
            }
            for (_, _, ans) in jobs
        ]