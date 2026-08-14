from __future__ import annotations

import gc
import hashlib
from collections.abc import Sequence
from dataclasses import replace

from .digest import DigestEntry

# Qwen3-Reranker ships a generic prompt ("Given a web search query, retrieve relevant passages
# that answer the query") in its default_prompt_name slot, and sentence-transformers applies it to
# every call. Leaving it in place puts the ranking criteria in the *query* slot instead of the
# instruction slot, so the model scores documents on how well they answer the criteria text: an
# item is then rewarded for containing the very words the criteria demote. Overriding the prompt
# puts the criteria where the model expects them and leaves the query as a short question.
RERANKER_PROMPT_NAME = "technical_digest"
RERANK_INSTRUCTION = (
    "Judge whether the document is a concrete, newsworthy technical development that belongs in a "
    "high-signal daily digest for AI, cybersecurity, and software engineering. Answer yes for specific "
    "releases, research results, security advisories, outages, and engineering changes with practical "
    "impact. Answer no for promotions, sponsored content, free trials, events, job posts, policy or "
    "landing pages, and vague trend commentary."
)
RERANK_QUERY = "Is this a concrete technical development worth reading today?"
# Derived from the prompt text so the recorded version cannot drift from what produced a score.
# Editing either string changes this automatically; a hand-maintained version string would not.
RERANKER_PROMPT_VERSION = hashlib.sha256(f"{RERANK_INSTRUCTION}\n{RERANK_QUERY}".encode()).hexdigest()[:12]


class RelevanceReranker:
    def __init__(self, model_name: str, device: str = "cpu") -> None:
        import torch
        from sentence_transformers import CrossEncoder

        self.model_name = model_name
        self.prompt_version = RERANKER_PROMPT_VERSION
        # The model declares Identity, so predict() returns the raw logit(yes) - logit(no)
        # difference. Sigmoid turns that into P(yes), which is the score the model documents and a
        # bounded, comparable number to persist. It is monotonic, so ordering is unaffected.
        self._activation_fn = torch.nn.Sigmoid()
        self._model = CrossEncoder(
            model_name,
            device=device,
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
        scores = self._model.predict(pairs, activation_fn=self._activation_fn)
        scored = [(float(score), entry) for score, entry in zip(scores, entries, strict=True)]
        return [replace(entry, reranker_score=score) for score, entry in sorted(scored, key=lambda value: value[0], reverse=True)]

    def close(self) -> None:
        del self._model
        gc.collect()
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
