from __future__ import annotations

import gc
from collections.abc import Sequence

from .digest import DigestEntry

RERANK_QUERY = (
    "Rank this newsletter item for a high-signal technical daily digest. Prefer concrete new developments with "
    "practical impact in AI, cybersecurity, or software engineering. Demote promotions, policy pages, free trials, "
    "events, job posts, generic roundups, and duplicates."
)


class RelevanceReranker:
    def __init__(self, model_name: str) -> None:
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(model_name)

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
        return [entry for _, entry in sorted(zip(scores, entries, strict=True), key=lambda value: float(value[0]), reverse=True)]

    def close(self) -> None:
        del self._model
        gc.collect()
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
