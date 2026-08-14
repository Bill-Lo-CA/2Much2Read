from __future__ import annotations

import gc
import hashlib
from collections.abc import Sequence
from dataclasses import replace

from .digest import DigestEntry

RERANK_QUERY = (
    "Rank this newsletter item for a high-signal technical daily digest. Prefer concrete new developments with "
    "practical impact in AI, cybersecurity, or software engineering. Demote promotions, policy pages, free trials, "
    "events, job posts, generic roundups, and duplicates."
)
# Derived from the query text so the recorded version cannot drift from the prompt that produced
# a score. Editing RERANK_QUERY changes this automatically; a hand-maintained string would not.
RERANKER_PROMPT_VERSION = hashlib.sha256(RERANK_QUERY.encode()).hexdigest()[:12]


class RelevanceReranker:
    def __init__(self, model_name: str, device: str = "cpu") -> None:
        from sentence_transformers import CrossEncoder

        self.model_name = model_name
        self.prompt_version = RERANKER_PROMPT_VERSION
        self._model = CrossEncoder(model_name, device=device)

    def rank(self, entries: Sequence[DigestEntry]) -> list[DigestEntry]:
        pairs = [
            (
                RERANK_QUERY,
                "\n".join(
                    (
                        f"Title: {entry.item.title}",
                        f"Summary: {entry.item.summary_zh_tw}",
                        f"Why it matters: {entry.item.why_it_matters_zh_tw}",
                        f"Source: {entry.source_name or entry.source_id or 'Unknown'}",
                    )
                ),
            )
            for entry in entries
        ]
        scores = self._model.predict(pairs)
        scored = [(float(score), entry) for score, entry in zip(scores, entries, strict=True)]
        return [replace(entry, reranker_score=score) for score, entry in sorted(scored, key=lambda value: value[0], reverse=True)]

    def close(self) -> None:
        del self._model
        gc.collect()
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
