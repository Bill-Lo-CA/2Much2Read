from __future__ import annotations

import pytest

from two_much_two_read import pipeline
from two_much_two_read.config import Settings
from two_much_two_read.digest import DigestEntry
from two_much_two_read.reranker import RelevanceReranker
from two_much_two_read.schemas import DigestItem, DigestReview


def entry(candidate_id: int, title: str, source_name: str) -> DigestEntry:
    return DigestEntry(
        DigestItem(
            title=title,
            category="AI_MODEL",
            summary_zh_tw="摘要",
            why_it_matters_zh_tw="重要原因",
            importance=5,
            confidence=0.1,
        ),
        candidate_id=candidate_id,
        source_id=source_name.casefold(),
        source_name=source_name,
    )


def test_reranker_orders_by_model_score() -> None:
    class FakeModel:
        def predict(self, pairs: list[tuple[str, str]], activation_fn) -> list[float]:
            assert "AlphaSignal" in pairs[0][1]
            assert activation_fn is not None
            return [0.2, 0.9]

    reranker = object.__new__(RelevanceReranker)
    reranker._model = FakeModel()  # type: ignore[attr-defined]
    reranker._sigmoid = object()  # type: ignore[attr-defined]

    ranked = reranker.rank([entry(1, "Trial", "AlphaSignal"), entry(2, "Release", "TLDR AI")])
    assert [value.candidate_id for value in ranked] == [2, 1]
    assert [value.reranker_score for value in ranked] == [0.9, 0.2]


def test_reranker_configures_custom_prompt_and_sigmoid(monkeypatch) -> None:
    import sentence_transformers

    captured: dict[str, object] = {}

    class FakeModel:
        def __init__(self, model_name: str, **kwargs: object) -> None:
            captured["model_name"] = model_name
            captured.update(kwargs)

        def predict(self, pairs, activation_fn):
            captured["activation_fn"] = activation_fn
            return [0.75 for _ in pairs]

    monkeypatch.setattr(sentence_transformers, "CrossEncoder", FakeModel)
    reranker = RelevanceReranker("Qwen/test")
    ranked = reranker.rank([entry(1, "Release", "TLDR AI")])

    assert captured["model_name"] == "Qwen/test"
    assert captured["default_prompt_name"] == "technical_digest"
    assert captured["prompts"]
    assert captured["activation_fn"] is not None
    assert ranked[0].reranker_score == 0.75


@pytest.mark.parametrize("score", [float("nan"), -0.1, 1.1])
def test_reranker_rejects_invalid_normalized_scores(score: float) -> None:
    class FakeModel:
        def predict(self, _pairs: list[tuple[str, str]], activation_fn) -> list[float]:
            return [score]

    reranker = object.__new__(RelevanceReranker)
    reranker._model = FakeModel()  # type: ignore[attr-defined]
    reranker._sigmoid = object()  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="reranker returned"):
        reranker.rank([entry(1, "Release", "TLDR AI")])


def test_final_review_selects_scored_items_and_releases_the_reviewer() -> None:
    class FakeOllama:
        review_model = "qwen3:8b"

        def __init__(self) -> None:
            self.candidates: list[dict[str, object]] = []
            self.unloaded: list[str] = []

        def review_digest(self, candidates: list[dict[str, object]], maximum: int) -> DigestReview:
            assert maximum == 1
            self.candidates = candidates
            return DigestReview.model_validate({"selected": [{"candidate_id": 2, "score": 90, "reason_zh_tw": "具體發布"}]})

        def unload(self, model: str) -> None:
            self.unloaded.append(model)

    ollama = FakeOllama()
    settings = Settings(digest_max_items=1, digest_review_candidate_limit=2, ollama_review_model="qwen3:8b")

    reviewed = pipeline._reviewed_entries(settings, ollama, [entry(2, "Release", "TLDR AI"), entry(1, "Trial", "AlphaSignal")])

    assert [(value.candidate_id, value.review_score) for value in reviewed] == [(2, 90)]
    assert ollama.candidates[0]["source"] == "TLDR AI"
    assert ollama.unloaded == ["qwen3:8b"]
