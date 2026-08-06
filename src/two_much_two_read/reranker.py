from __future__ import annotations

import gc
import math
from collections.abc import Sequence
from dataclasses import replace
from typing import SupportsFloat, cast

from .digest import DigestEntry

RERANKER_PROMPT_VERSION = "technical-digest-v1"
RERANKER_PROMPT_NAME = "technical_digest"
RERANK_INSTRUCTION = (
    "Judge whether a document is valuable for a high-signal technical daily digest. "
    "Prioritize concrete, recent releases, research results, security incidents, standards, or engineering changes "
    "with clear implications for AI, cybersecurity, or software engineering practice. "
    "Reward specific technical details and actionable impact. "
    "Strongly down-rank advertisements, sponsored content, free trials, events, job posts, generic commentary, "
    "policy or landing pages, and vague trend pieces. "
    "Do not reward a source merely for being famous; score the document's substance and relevance."
)
RERANK_QUERY = "Should this document be included in today's high-signal technical engineering digest?"


class RelevanceReranker:
    def __init__(self, model_name: str) -> None:
        from sentence_transformers import CrossEncoder

        self.model_name = model_name
        self.prompt_version = RERANKER_PROMPT_VERSION
        import torch

        self._sigmoid = torch.nn.Sigmoid()
        self._model = CrossEncoder(
            model_name,
            prompts={RERANKER_PROMPT_NAME: RERANK_INSTRUCTION},
            default_prompt_name=RERANKER_PROMPT_NAME,
        )

    def rank(self, entries: Sequence[DigestEntry]) -> list[DigestEntry]:
        pairs = [
            (
                RERANK_QUERY,
                "\n".join(
                    (
                        f"Title: {entry.item.title}",
                        f"Category: {entry.item.category}",
                        f"Summary: {entry.item.summary_zh_tw}",
                        f"Why it matters: {entry.item.why_it_matters_zh_tw}",
                        f"Source: {entry.source_name or entry.source_id or 'Unknown'}",
                    )
                ),
            )
            for entry in entries
        ]
        scores = self._model.predict(pairs, activation_fn=self._sigmoid)
        scored = [(_score_01(score), entry) for score, entry in zip(scores, entries, strict=True)]
        return [replace(entry, reranker_score=score) for score, entry in sorted(scored, key=lambda value: value[0], reverse=True)]

    def close(self) -> None:
        del self._model
        gc.collect()
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def _score_01(value: object) -> float:
    score = float(cast(SupportsFloat, value))
    if not math.isfinite(score):
        raise ValueError(f"reranker returned non-finite score: {score!r}")
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"reranker returned score outside 0..1: {score!r}")
    return score
