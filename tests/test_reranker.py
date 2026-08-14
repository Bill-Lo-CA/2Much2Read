from __future__ import annotations

import hashlib
import sys
import types
from dataclasses import replace

import pytest

from two_much_two_read import pipeline
from two_much_two_read.config import Settings
from two_much_two_read.digest import DigestEntry
from two_much_two_read.reranker import RERANK_QUERY, RERANKER_PROMPT_VERSION, RelevanceReranker
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
        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            assert "AlphaSignal" in pairs[0][1]
            return [0.2, 0.9]

    reranker = object.__new__(RelevanceReranker)
    reranker._model = FakeModel()  # type: ignore[attr-defined]

    ranked = reranker.rank([entry(1, "Trial", "AlphaSignal"), entry(2, "Release", "TLDR AI")])
    assert [value.candidate_id for value in ranked] == [2, 1]


def test_reranker_loads_on_the_configured_device(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeCrossEncoder:
        def __init__(self, model_name_or_path: str, device: str) -> None:
            captured["model"] = model_name_or_path
            captured["device"] = device

    module = types.ModuleType("sentence_transformers")
    module.CrossEncoder = FakeCrossEncoder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    RelevanceReranker("Qwen/test")
    assert captured == {"model": "Qwen/test", "device": "cpu"}

    RelevanceReranker("Qwen/test", "cuda")
    assert captured["device"] == "cuda"


def test_reranker_defaults_to_cpu_so_it_never_competes_with_the_reviewer() -> None:
    assert Settings().reranker_device == "cpu"


def test_reranker_attaches_the_raw_model_score() -> None:
    class FakeModel:
        def predict(self, _pairs: list[tuple[str, str]]) -> list[float]:
            return [0.2, 4.7]

    reranker = object.__new__(RelevanceReranker)
    reranker._model = FakeModel()  # type: ignore[attr-defined]

    ranked = reranker.rank([entry(1, "Trial", "AlphaSignal"), entry(2, "Release", "TLDR AI")])

    assert [(value.candidate_id, value.reranker_score) for value in ranked] == [(2, 4.7), (1, 0.2)]


def test_reranker_prompt_version_is_derived_from_the_query_text() -> None:
    assert hashlib.sha256(RERANK_QUERY.encode()).hexdigest()[:12] == RERANKER_PROMPT_VERSION


def test_ranked_entries_keeps_every_candidate_so_the_audit_covers_them_all() -> None:
    class FakeReranker:
        def rank(self, entries):
            return list(entries)

    entries = [entry(index, f"Story {index}", "TLDR AI") for index in range(1, 6)]

    ranked = pipeline._ranked_entries(FakeReranker(), entries)

    assert [value.candidate_id for value in ranked] == [1, 2, 3, 4, 5]


def test_unload_failure_is_reported_to_the_status_reporter() -> None:
    class FakeOllama:
        def unload(self, _model: str) -> bool:
            return False

    messages: list[str] = []

    pipeline._unload_model(FakeOllama(), "qwen3:8b", messages.append)

    assert messages == ["Warning: qwen3:8b did not unload and may still hold memory"]


def test_unwritten_reranker_scores_are_reported() -> None:
    class FakeDatabase:
        def save_reranker_scores(self, scores, model, prompt_version):
            return 1

    class FakeReranker:
        model_name = "Qwen/test"
        prompt_version = "v1"

    messages: list[str] = []
    ranked = [
        replace(entry(1, "First", "TLDR AI"), reranker_score=0.9),
        replace(entry(2, "Second", "TLDR AI"), reranker_score=0.1),
    ]

    pipeline._save_reranker_scores(FakeDatabase(), ranked, FakeReranker(), messages.append)

    assert messages == ["Warning: recorded 1 of 2 reranker scores"]


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
