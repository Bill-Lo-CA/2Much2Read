from __future__ import annotations

import hashlib
import sys
import types
from dataclasses import replace

import pytest

from two_much_two_read import pipeline
from two_much_two_read.config import Settings
from two_much_two_read.digest import DigestEntry
from two_much_two_read.reranker import (
    RERANK_INSTRUCTION,
    RERANK_QUERY,
    RERANKER_PROMPT_NAME,
    RERANKER_PROMPT_VERSION,
    RelevanceReranker,
)
from two_much_two_read.schemas import DigestItem, DigestReview


def entry(candidate_id: int, title: str, source_name: str, category: str = "AI_MODEL") -> DigestEntry:
    return DigestEntry(
        DigestItem(
            title=title,
            category=category,
            summary_zh_tw="摘要",
            why_it_matters_zh_tw="重要原因",
            importance=5,
            confidence=0.1,
        ),
        candidate_id=candidate_id,
        source_id=source_name.casefold(),
        source_name=source_name,
    )


def fake_reranker(scores: list[float]) -> RelevanceReranker:
    class FakeModel:
        def predict(self, pairs: list[tuple[str, str]], activation_fn: object) -> list[float]:
            assert activation_fn is not None
            assert pairs[0][0] == RERANK_QUERY
            return scores

    reranker = object.__new__(RelevanceReranker)
    reranker._model = FakeModel()  # type: ignore[attr-defined]
    reranker._activation_fn = object()  # type: ignore[attr-defined]
    return reranker


def test_reranker_orders_by_model_score() -> None:
    reranker = fake_reranker([0.2, 0.9])

    ranked = reranker.rank([entry(1, "Trial", "AlphaSignal"), entry(2, "Release", "TLDR AI")])

    assert [(value.candidate_id, value.reranker_score) for value in ranked] == [(2, 0.9), (1, 0.2)]


def test_reranker_overrides_the_models_generic_search_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Qwen3-Reranker defaults to a generic web-search instruction.

    Leaving it in place puts the ranking criteria in the query slot, which inverts the ranking:
    an item scores high for containing the very words the criteria demote.
    """
    captured: dict[str, object] = {}

    class FakeCrossEncoder:
        def __init__(self, model_name_or_path: str, **kwargs: object) -> None:
            captured["model"] = model_name_or_path
            captured.update(kwargs)

    module = types.ModuleType("sentence_transformers")
    module.CrossEncoder = FakeCrossEncoder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    RelevanceReranker("Qwen/test")

    assert captured["model"] == "Qwen/test"
    assert captured["device"] == "cpu"
    assert captured["default_prompt_name"] == RERANKER_PROMPT_NAME
    assert captured["prompts"] == {RERANKER_PROMPT_NAME: RERANK_INSTRUCTION}

    RelevanceReranker("Qwen/test", "cuda")
    assert captured["device"] == "cuda"


def test_reranker_defaults_to_cpu_so_it_never_competes_with_the_reviewer() -> None:
    assert Settings().reranker_device == "cpu"


def test_reranker_prompt_version_covers_both_the_instruction_and_the_query() -> None:
    expected = hashlib.sha256(f"{RERANK_INSTRUCTION}\n{RERANK_QUERY}".encode()).hexdigest()[:12]

    assert expected == RERANKER_PROMPT_VERSION


def test_ranked_entries_keeps_every_candidate_so_the_audit_covers_them_all() -> None:
    class FakeReranker:
        def rank(self, entries):
            return list(entries)

    entries = [entry(index, f"Story {index}", "TLDR AI") for index in range(1, 6)]

    ranked = pipeline._ranked_entries(FakeReranker(), entries)

    assert [value.candidate_id for value in ranked] == [1, 2, 3, 4, 5]


def ranked_pair(security: int, general: int) -> list[DigestEntry]:
    """Every security item ranks below every general item, as the reranker orders them in practice."""
    entries = [entry(index, f"Release {index}", "TLDR AI") for index in range(general)]
    return entries + [entry(100 + index, f"CVE {index}", "TLDR Sec", "SECURITY") for index in range(security)]


def test_security_candidates_reach_the_reviewer_from_below_the_global_cutoff() -> None:
    ranked = ranked_pair(security=9, general=30)

    kept = pipeline._review_candidates(ranked, 20, 7)

    assert len(kept) == 20
    assert [value.candidate_id for value in kept if value.item.category == "SECURITY"] == list(range(100, 107))
    assert [value.candidate_id for value in kept if value.item.category != "SECURITY"] == list(range(13))


def test_unused_security_slots_go_to_the_other_categories() -> None:
    ranked = ranked_pair(security=2, general=30)

    kept = pipeline._review_candidates(ranked, 20, 7)

    assert len(kept) == 20
    assert sum(1 for value in kept if value.item.category == "SECURITY") == 2


def test_unused_general_slots_go_to_security() -> None:
    ranked = ranked_pair(security=30, general=5)

    kept = pipeline._review_candidates(ranked, 20, 7)

    assert len(kept) == 20
    assert sum(1 for value in kept if value.item.category == "SECURITY") == 15


def test_the_quota_never_exceeds_the_reviewer_limit() -> None:
    ranked = ranked_pair(security=30, general=30)

    assert len(pipeline._review_candidates(ranked, 20, 40)) == 20


def test_kept_candidates_stay_in_reranker_order() -> None:
    ranked = [
        entry(1, "Release", "TLDR AI"),
        entry(2, "CVE", "TLDR Sec", "SECURITY"),
        entry(3, "Another release", "TLDR AI"),
    ]

    assert [value.candidate_id for value in pipeline._review_candidates(ranked, 3, 1)] == [1, 2, 3]


def test_security_slots_default_to_seven_of_the_twenty_reviewer_slots() -> None:
    settings = Settings()

    assert (settings.digest_review_candidate_limit, settings.digest_security_candidate_slots) == (20, 7)


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


def test_final_review_selects_scored_items_and_leaves_the_reviewer_loaded() -> None:
    class FakeOllama:
        review_model = "qwen3:8b"

        def __init__(self) -> None:
            self.candidates: list[dict[str, object]] = []
            self.unloaded: list[str] = []

        def review_digest(self, candidates: list[dict[str, object]], maximum: int, *_: object) -> DigestReview:
            assert maximum == 1
            self.candidates = candidates
            return DigestReview.model_validate({"selected": [{"candidate_id": 2, "score": 90, "reason_zh_tw": "具體發布"}]})

        def unload(self, model: str) -> None:
            self.unloaded.append(model)

    ollama = FakeOllama()
    settings = Settings(digest_max_items=1, digest_review_candidate_limit=2, ollama_review_model="qwen3:8b")

    reviewed = pipeline._reviewed_entries(settings, ollama, [entry(2, "Release", "TLDR AI"), entry(1, "Trial", "AlphaSignal")])

    assert [(value.candidate_id, value.review_score) for value in reviewed] == [(2, 90), (1, None)]
    assert ollama.candidates[0]["source"] == "TLDR AI"
    # The headline rewrite runs on the same model, so run_pipeline releases it, not this step.
    assert ollama.unloaded == []


def test_candidates_the_reviewer_passed_over_become_secondary_mentions() -> None:
    class FakeOllama:
        def review_digest(self, candidates: list[dict[str, object]], maximum: int, *_: object) -> DigestReview:
            return DigestReview.model_validate({"selected": [{"candidate_id": 1, "score": 90, "reason_zh_tw": "具體發布"}]})

        def unload(self, _model: str) -> bool:
            return True

    settings = Settings(digest_max_items=1, digest_review_candidate_limit=5, digest_secondary_items=2)
    ranked = [entry(index, f"Story {index}", "TLDR AI") for index in range(1, 6)]

    reviewed = pipeline._reviewed_entries(settings, FakeOllama(), ranked)

    # Every passed-over candidate is returned; the secondary limit is applied after merging, so a
    # mention absorbed into a headline frees its slot for the next one rather than shrinking the list.
    assert [(value.candidate_id, value.review_score) for value in reviewed] == [
        (1, 90),
        (2, None),
        (3, None),
        (4, None),
        (5, None),
    ]
    assert [value.candidate_id for value in pipeline._merged_entries(reviewed, 0.25, 2, ranked, "zh-TW")] == [1, 2, 3]


def test_secondary_mentions_can_be_turned_off() -> None:
    class FakeOllama:
        def review_digest(self, candidates: list[dict[str, object]], maximum: int, *_: object) -> DigestReview:
            return DigestReview.model_validate({"selected": [{"candidate_id": 1, "score": 90, "reason_zh_tw": "具體發布"}]})

        def unload(self, _model: str) -> bool:
            return True

    settings = Settings(digest_max_items=1, digest_secondary_items=0)
    ranked = [entry(index, f"Story {index}", "TLDR AI") for index in range(1, 4)]
    reviewed = pipeline._reviewed_entries(settings, FakeOllama(), ranked)

    assert [value.candidate_id for value in pipeline._merged_entries(reviewed, 0.25, 0, ranked, "zh-TW")] == [1]
