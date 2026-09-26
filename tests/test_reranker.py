from __future__ import annotations

import hashlib
import sys
import types
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from two_much_two_read import pipeline
from two_much_two_read.command_models import SecurityFloorPromotion
from two_much_two_read.config import Settings
from two_much_two_read.digest import (
    DigestEntry,
    _entry_rank,
    dedupe_entries,
    has_source_text,
    merge_related_entries,
    render_digest,
    with_previous_coverage,
)
from two_much_two_read.reranker import (
    RERANK_INSTRUCTION,
    RERANK_QUERY,
    RERANKER_PROMPT_NAME,
    RERANKER_PROMPT_VERSION,
    RelevanceReranker,
)
from two_much_two_read.schemas import DigestCategory, DigestItem, DigestReview


def entry(candidate_id: int, title: str, source_name: str, category: DigestCategory = "AI_MODEL") -> DigestEntry:
    """A candidate with an article behind it, as nearly every one is once its link is matched."""
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
        article_url=f"https://example.com/story-{candidate_id}",
    )


def fake_reranker(scores: list[float]) -> RelevanceReranker:
    class FakeModel:
        def predict(self, pairs: list[tuple[str, str]], activation_fn: object) -> list[float]:
            assert activation_fn is not None
            assert pairs[0][0] == RERANK_QUERY
            return scores

    reranker = object.__new__(RelevanceReranker)
    reranker._model = FakeModel()
    reranker._activation_fn = object()
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
    assert captured.get("device") == "cuda"


def test_reranker_defaults_to_cpu_so_it_never_competes_with_the_reviewer() -> None:
    assert Settings().reranker_device == "cpu"


def test_reranker_prompt_version_covers_both_the_instruction_and_the_query() -> None:
    expected = hashlib.sha256(f"{RERANK_INSTRUCTION}\n{RERANK_QUERY}".encode()).hexdigest()[:12]

    assert expected == RERANKER_PROMPT_VERSION


def test_ranked_entries_keeps_every_candidate_so_the_audit_covers_them_all() -> None:
    class FakeReranker:
        def rank(self, entries: Sequence[DigestEntry]) -> list[DigestEntry]:
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
        def save_reranker_scores(self, scores: Sequence[tuple[int, float]], model: str, prompt_version: str) -> int:
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
    settings = Settings(
        digest_max_items=1, digest_review_candidate_limit=2, digest_headlines_per_source=0, ollama_review_model="qwen3:8b"
    )

    reviewed = pipeline._reviewed_entries(settings, ollama, [entry(2, "Release", "TLDR AI"), entry(1, "Trial", "AlphaSignal")])

    assert [(value.candidate_id, value.review_score) for value in reviewed] == [(2, 90), (1, None)]
    assert ollama.candidates[0]["source"] == "TLDR AI"
    # The headline rewrite runs on the same model, so run_pipeline releases it, not this step.
    assert ollama.unloaded == []


def never_the_same(_left: DigestEntry, _right: DigestEntry) -> bool:
    """These fixtures are unrelated stories; the model would say so."""
    return False


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
    assert [value.candidate_id for value in pipeline._merged_entries(reviewed, 2, never_the_same)[0]] == [1, 2, 3]


def test_secondary_mentions_can_be_turned_off() -> None:
    class FakeOllama:
        def review_digest(self, candidates: list[dict[str, object]], maximum: int, *_: object) -> DigestReview:
            return DigestReview.model_validate({"selected": [{"candidate_id": 1, "score": 90, "reason_zh_tw": "具體發布"}]})

        def unload(self, _model: str) -> bool:
            return True

    settings = Settings(digest_max_items=1, digest_secondary_items=0)
    ranked = [entry(index, f"Story {index}", "TLDR AI") for index in range(1, 4)]
    reviewed = pipeline._reviewed_entries(settings, FakeOllama(), ranked)

    assert [value.candidate_id for value in pipeline._merged_entries(reviewed, 0, never_the_same)[0]] == [1]


def _digest(headlines: Sequence[tuple[str, DigestCategory]], mentions: Sequence[tuple[str, DigestCategory]]) -> list[DigestEntry]:
    """Reviewed headlines scored 90, 80, ... and passed-over mentions in falling reranker order."""
    reviewed = [
        replace(entry(index, title, "TLDR", category), review_score=90 - 10 * index, reranker_score=0.9)
        for index, (title, category) in enumerate(headlines)
    ]
    passed_over = [
        replace(entry(100 + index, title, "TLDR", category), reranker_score=0.5 - 0.01 * index)
        for index, (title, category) in enumerate(mentions)
    ]
    return reviewed + passed_over


def _floored(entries: list[DigestEntry], headline_limit: int, secondary_items: int = 10) -> tuple[list[str], list[str]]:
    result, _ = pipeline._merged_entries(entries, secondary_items, never_the_same, headline_limit=headline_limit)
    return (
        [value.item.title for value in result if value.review_score is not None],
        [value.item.title for value in result if value.review_score is None],
    )


def test_a_digest_without_security_headlines_promotes_the_best_security_mention() -> None:
    # 2026-09-24: thirteen SECURITY candidates reached the reviewer and it chose none of the ones
    # that mattered, a 0-day it had itself rated 9 of 10 among them.
    entries = _digest(
        [("Opus 5.5", "AI_MODEL"), ("GPT-6", "AI_MODEL"), ("Qwen image", "AI_MODEL")],
        [("Rune IDE", "DEV_TOOL"), ("Muse 0-day", "SECURITY"), ("Clop leak site", "SECURITY")],
    )

    headlines, mentions = _floored(entries, headline_limit=3)

    assert headlines == ["Opus 5.5", "GPT-6", "Muse 0-day"]
    assert mentions == ["Qwen image", "Rune IDE", "Clop leak site"]


def test_the_promoted_story_renders_as_the_last_headline() -> None:
    entries = _digest(
        [("Opus 5.5", "AI_MODEL"), ("GPT-6", "AI_MODEL")],
        [("Rune IDE", "DEV_TOOL"), ("Muse 0-day", "SECURITY")],
    )

    content = render_digest(
        pipeline._merged_entries(entries, 10, never_the_same, headline_limit=2)[0], datetime(2026, 9, 24), "AI", "TLDR", 2
    )

    top, rest = content.split("🧰")
    assert "1. Opus 5.5" in top
    assert "2. Muse 0-day" in top
    assert "GPT-6" in rest


def test_a_security_headline_already_satisfies_the_floor() -> None:
    entries = _digest([("Opus 5.5", "AI_MODEL"), ("Docker escape", "SECURITY")], [("Muse 0-day", "SECURITY")])

    assert _floored(entries, headline_limit=2) == (["Opus 5.5", "Docker escape"], ["Muse 0-day"])


def test_a_cve_among_the_shown_mentions_satisfies_the_floor() -> None:
    # A CVE is a one-line fact, so a mention is where it belongs.
    entries = _digest(
        [("Opus 5.5", "AI_MODEL"), ("GPT-6", "AI_MODEL")],
        [("Docker patches CVE-2026-77179", "SECURITY"), ("Muse 0-day", "SECURITY")],
    )

    assert _floored(entries, headline_limit=2) == (
        ["Opus 5.5", "GPT-6"],
        ["Docker patches CVE-2026-77179", "Muse 0-day"],
    )


def test_a_cve_the_mention_cap_cuts_does_not_count() -> None:
    entries = _digest(
        [("Opus 5.5", "AI_MODEL"), ("GPT-6", "AI_MODEL")],
        [("Rune IDE", "DEV_TOOL"), ("Docker patches CVE-2026-77179", "SECURITY")],
    )

    headlines, mentions = _floored(entries, headline_limit=2, secondary_items=1)

    # Promoted from beyond the cap: the floor looks at every passed-over candidate, not only the shown
    # ones. GPT-6 was the reviewer's choice, so it sits outside the one-mention quota Rune IDE fills.
    assert headlines == ["Opus 5.5", "Docker patches CVE-2026-77179"]
    assert mentions == ["GPT-6", "Rune IDE"]


def test_a_short_headline_list_gains_the_security_story_without_losing_one() -> None:
    entries = _digest([("Opus 5.5", "AI_MODEL")], [("Rune IDE", "DEV_TOOL"), ("Muse 0-day", "SECURITY")])

    assert _floored(entries, headline_limit=3) == (["Opus 5.5", "Muse 0-day"], ["Rune IDE"])


def test_a_pool_without_security_is_left_alone() -> None:
    entries = _digest([("Opus 5.5", "AI_MODEL"), ("GPT-6", "AI_MODEL")], [("Rune IDE", "DEV_TOOL")])

    assert _floored(entries, headline_limit=2) == (["Opus 5.5", "GPT-6"], ["Rune IDE"])


@pytest.mark.parametrize(
    ("mentions", "expected_mentions"),
    [
        ([("Muse 0-day", "SECURITY")], ["GPT-6", "Muse 0-day"]),
        ([("Rune IDE", "DEV_TOOL")], ["GPT-6", "Rune IDE"]),
    ],
)
def test_a_security_story_the_reviewer_chose_is_promoted_before_one_it_passed_over(
    mentions: list[tuple[str, DigestCategory]], expected_mentions: list[str]
) -> None:
    # With DIGEST_MAX_ITEMS above DIGEST_TOP_ITEMS the reviewer can choose a security story that
    # ranks past the headlines the renderer shows. It is the one to surface: a passed-over story in
    # its place overrules the reviewer, and with no other security story the digest showed none.
    entries = _digest([("Opus 5.5", "AI_MODEL"), ("GPT-6", "AI_MODEL"), ("Docker escape", "SECURITY")], mentions)

    headlines, shown = _floored(entries, headline_limit=2)

    assert headlines == ["Opus 5.5", "Docker escape"]
    assert shown == expected_mentions


def test_the_promoted_story_renders_after_a_headline_the_reviewer_scored_zero() -> None:
    # The reviewer may give 0. A promoted story tied with it on 0 fell through to the reranker and,
    # scored higher there, rendered ahead of the reviewer's own choice. Below 0 it would sort among
    # the mentions instead, and render_digest reads the mentions as what follows the headlines: the
    # promoted story rendered twice and Rune IDE, ranked above it, was cut.
    entries = [
        replace(entry(1, "Opus 5.5", "TLDR"), review_score=90, reranker_score=0.9),
        replace(entry(2, "Reviewer's zero", "TLDR"), review_score=0, reranker_score=0.1),
        replace(entry(3, "Rune IDE", "TLDR", "DEV_TOOL"), reranker_score=0.6),
        replace(entry(4, "Muse 0-day", "TLDR", "SECURITY"), reranker_score=0.5),
    ]

    content = render_digest(
        pipeline._merged_entries(entries, 10, never_the_same, headline_limit=3)[0], datetime(2026, 9, 24), "AI", "TLDR", 3
    )

    top, rest = content.split("🧰")
    assert [line.split(" ", 1)[1] for line in top.splitlines() if line[:2] in ("1.", "2.", "3.")] == [
        "Opus 5.5",
        "Reviewer's zero",
        "Muse 0-day",
    ]
    assert [line for line in rest.splitlines() if line.startswith("•")] == ["• Rune IDE · TLDR · <https://example.com/story-3>"]


@pytest.mark.parametrize(
    "summary",
    ["Docker 修復 CVE-2026-77179 漏洞", "Docker 修復CVE-2026-77179漏洞", "（CVE-2026-77179）"],
)
def test_a_cve_is_recognised_however_the_summary_spaces_it(summary: str) -> None:
    # \b would miss the middle one: CJK characters are word characters to Python's re.
    cve = entry(1, "Docker 沙盒漏洞", "TLDR", "SECURITY")
    cve = replace(cve, item=cve.item.model_copy(update={"summary_zh_tw": summary}))

    assert pipeline._is_cve(cve)


@pytest.mark.parametrize("title", ["XCVE-2026-77179 bundle", "CVE-2026-77179A patch", "CVE-2026-77179based scanner"])
def test_a_cve_like_token_inside_another_word_is_not_a_cve(title: str) -> None:
    # Either side: a lookalike in a shown mention would satisfy the floor and leave no security headline.
    lookalike = entry(1, title, "TLDR", "SECURITY")

    assert not pipeline._is_cve(lookalike)


def test_the_floor_reports_what_it_promoted_and_what_made_room() -> None:
    entries = _digest(
        [("Opus 5.5", "AI_MODEL"), ("GPT-6", "AI_MODEL")],
        [("Rune IDE", "DEV_TOOL"), ("Muse 0-day", "SECURITY")],
    )

    _, promotion = pipeline._merged_entries(entries, 10, never_the_same, headline_limit=2)

    assert promotion == SecurityFloorPromotion(promoted="Muse 0-day", source="TLDR", displaced="GPT-6")


def test_a_floor_that_only_filled_an_empty_slot_displaced_nothing() -> None:
    entries = _digest([("Opus 5.5", "AI_MODEL")], [("Muse 0-day", "SECURITY")])

    _, promotion = pipeline._merged_entries(entries, 10, never_the_same, headline_limit=3)

    assert promotion == SecurityFloorPromotion(promoted="Muse 0-day", source="TLDR", displaced=None)


@pytest.mark.parametrize(
    ("headlines", "mentions"),
    [
        ([("Docker escape", "SECURITY")], [("Muse 0-day", "SECURITY")]),
        ([("Opus 5.5", "AI_MODEL")], [("Docker patches CVE-2026-77179", "SECURITY")]),
        ([("Opus 5.5", "AI_MODEL")], [("Rune IDE", "DEV_TOOL")]),
    ],
)
def test_a_floor_that_did_not_act_reports_nothing(
    headlines: list[tuple[str, DigestCategory]], mentions: list[tuple[str, DigestCategory]]
) -> None:
    _, promotion = pipeline._merged_entries(_digest(headlines, mentions), 10, never_the_same, headline_limit=1)

    assert promotion is None


def test_a_digest_the_reviewer_chose_nothing_for_keeps_its_ranked_headlines() -> None:
    # render_digest falls back to the ranked list while no entry has a review score. Promoting one
    # ended that fallback: three headlines became one, and the other two were pushed into mentions.
    entries = _digest([], [("Opus 5.5", "AI_MODEL"), ("GPT-6", "AI_MODEL"), ("Rune IDE", "DEV_TOOL"), ("Muse 0-day", "SECURITY")])

    merged, promotion = pipeline._merged_entries(entries, 10, never_the_same, headline_limit=3)
    content = render_digest(merged, datetime(2026, 9, 24), "AI", "TLDR", 3)

    top = content.split("🧰")[0]
    assert [line.split(" ", 1)[1] for line in top.splitlines() if line[:2] in ("1.", "2.", "3.")] == [
        "Opus 5.5",
        "GPT-6",
        "Rune IDE",
    ]
    assert promotion is None


def test_the_promotion_record_carries_no_terminal_controls() -> None:
    # The record is printed as JSON with ensure_ascii=False, which escapes only U+0000-U+001F.
    # U+009B is the C1 CSI and U+202E reverses the text after it.
    entries = [
        replace(entry(1, "Opus 5.5", "TLDR"), review_score=90, reranker_score=0.9),
        replace(entry(2, "Muse\x9b2J 0-day‮", "Risky\x9bBiz", "SECURITY"), reranker_score=0.5),
    ]

    _, promotion = pipeline._merged_entries(entries, 10, never_the_same, headline_limit=1)

    assert promotion == SecurityFloorPromotion(promoted="Muse 2J 0-day", source="Risky Biz", displaced="Opus 5.5")


def test_demoted_headlines_leave_the_passed_over_quota_alone() -> None:
    # DIGEST_MAX_ITEMS above DIGEST_TOP_ITEMS: the reviewer chose four, the renderer shows two. The
    # two past the cap rendered outside DIGEST_SECONDARY_ITEMS before the floor acted, and counting
    # them against it afterwards cut both passed-over mentions.
    entries = _digest(
        [("Opus 5.5", "AI_MODEL"), ("GPT-6", "AI_MODEL"), ("Qwen image", "AI_MODEL"), ("Gemini TTS", "AI_MODEL")],
        [("Rune IDE", "DEV_TOOL"), ("Drop sandbox", "DEV_TOOL"), ("Muse 0-day", "SECURITY")],
    )

    headlines, mentions = _floored(entries, headline_limit=2, secondary_items=2)

    assert headlines == ["Opus 5.5", "Muse 0-day"]
    assert mentions == ["GPT-6", "Qwen image", "Gemini TTS", "Rune IDE", "Drop sandbox"]


def _headline_only(candidate_id: int, title: str, category: DigestCategory = "AI_MODEL") -> DigestEntry:
    """A link-list item whose link was never matched: nothing behind it but the headline."""
    return replace(entry(candidate_id, title, "Hacker Newsletter", category), article_url=None, content_basis="newsletter")


def test_items_with_nothing_behind_them_are_kept_from_the_reviewer_but_stay_mentions() -> None:
    # With no article and no other coverage the summary can only restate or invent, so it may not
    # become a headline; it is still worth a line among the mentions, in the reranker's order.
    seen: list[object] = []

    class FakeOllama:
        def review_digest(self, candidates: list[dict[str, object]], maximum: int, *_: object) -> DigestReview:
            seen.extend(candidate["candidate_id"] for candidate in candidates)
            return DigestReview.model_validate({"selected": [{"candidate_id": 2, "score": 90, "reason_zh_tw": "具體"}]})

    ranked = [_headline_only(1, "Grok 4.7"), entry(2, "Opus 5.5 pricing", "AlphaSignal"), _headline_only(3, "GPT-6 Sol")]

    reviewed = pipeline._reviewed_entries(Settings(digest_max_items=1), FakeOllama(), ranked)

    assert seen == [2]
    assert [(value.candidate_id, value.review_score) for value in reviewed] == [(2, 90), (1, None), (3, None)]


def test_a_pool_of_only_headlines_skips_the_reviewer() -> None:
    class Unused:
        def review_digest(self, *_: object) -> DigestReview:
            raise AssertionError("nothing it could select may be a headline")

    ranked = [_headline_only(1, "Grok 4.7"), _headline_only(2, "GPT-6 Sol")]

    assert pipeline._reviewed_entries(Settings(), Unused(), ranked) == ranked


def test_the_security_floor_never_promotes_a_story_with_nothing_behind_it() -> None:
    headlines = [replace(entry(1, "Opus 5.5", "TLDR"), review_score=90, reranker_score=0.9)]
    muse = replace(_headline_only(2, "Muse 0-day", "SECURITY"), reranker_score=0.6)
    clop = replace(entry(3, "Clop leak site", "TLDR", "SECURITY"), reranker_score=0.4)

    promoted, _ = pipeline._merged_entries([*headlines, muse, clop], 10, never_the_same, headline_limit=2)
    alone, promotion = pipeline._merged_entries([*headlines, muse], 10, never_the_same, headline_limit=2)

    assert [value.item.title for value in promoted if value.review_score is not None] == ["Opus 5.5", "Clop leak site"]
    assert promotion is None
    assert [value.item.title for value in alone if value.review_score is not None] == ["Opus 5.5"]


def test_the_security_floor_never_promotes_coverage_gained_after_the_review() -> None:
    # Two bare copies of one 0-day from different newsletters: merged, the entry has another source's
    # coverage, but neither copy reached the reviewer, so the floor may not make it a headline. A
    # copy with an article did reach it, and when that copy takes over the merge it stays eligible.
    headlines = [replace(entry(1, "Opus 5.5", "TLDR"), review_score=90, reranker_score=0.9)]
    muse = replace(_headline_only(2, "Muse 0-day", "SECURITY"), reranker_score=0.6)
    muse_again = replace(_headline_only(3, "Meta Muse 0-day", "SECURITY"), source_name="Risky Business", reranker_score=0.5)
    muse_written = replace(entry(4, "Meta patches Muse 0-day", "SANS", "SECURITY"), reranker_score=0.4)

    def muse_is_muse(left: DigestEntry, right: DigestEntry) -> bool:
        return "Muse" in left.item.title and "Muse" in right.item.title

    bare, bare_promotion = pipeline._merged_entries([*headlines, muse, muse_again], 10, muse_is_muse, headline_limit=2)
    backed, backed_promotion = pipeline._merged_entries([*headlines, muse, muse_written], 10, muse_is_muse, headline_limit=2)

    assert bare_promotion is None
    assert [value.item.title for value in bare if value.review_score is not None] == ["Opus 5.5"]
    assert backed_promotion is not None and backed_promotion.promoted == "Meta patches Muse 0-day"


def test_the_copy_with_an_article_is_kept_when_a_bare_one_repeats_it() -> None:
    # Hacker Newsletter's "Grok 4.7" ranked above AlphaSignal's write-up of the same launch; kept as
    # the primary, its one guessed line would have replaced the real summary and its article.
    listed = replace(_headline_only(1, "Grok 4.7"), reranker_score=0.9)
    written = replace(entry(2, "xAI releases Grok 4.7", "AlphaSignal"), reranker_score=0.5)

    _, mentions = merge_related_entries([], [listed, written], lambda _left, _right: True)

    assert len(mentions) == 1
    assert (mentions[0].item.title, mentions[0].also_from) == ("xAI releases Grok 4.7", ("Hacker Newsletter",))


def test_a_story_keeps_its_higher_rank_when_its_backed_copy_takes_over() -> None:
    # The bare copy ranked first and the backed one last, with an unrelated story between. The merged
    # story holds the first slot, so it keeps the first slot's score: with the lower one, the mention
    # quota - which cuts in list order - would keep it over the unrelated story it now sorts below.
    listed = replace(_headline_only(1, "Grok 4.7"), reranker_score=0.9)
    unrelated = replace(entry(2, "Rust 2.0 ships", "TLDR"), reranker_score=0.7)
    written = replace(entry(3, "xAI releases Grok 4.7", "AlphaSignal"), reranker_score=0.3)

    _, mentions = merge_related_entries(
        [], [listed, unrelated, written], lambda left, right: {left.candidate_id, right.candidate_id} == {1, 3}
    )

    assert [(value.item.title, value.reranker_score) for value in mentions] == [
        ("xAI releases Grok 4.7", 0.9),
        ("Rust 2.0 ships", 0.7),
    ]
    assert sorted(mentions, key=_entry_rank, reverse=True) == mentions


def test_an_identical_bare_copy_loses_to_the_one_with_coverage_behind_it() -> None:
    # Same title and no article on either side, so dedupe_entries keys them together; the one other
    # newsletters also covered is kept, whatever the reranker thought of the two.
    listed = replace(_headline_only(1, "Grok 4.7"), reranker_score=0.9)
    covered = replace(
        _headline_only(2, "Grok 4.7"),
        source_name="AlphaSignal",
        reranker_score=0.5,
        also_from=("TLDR AI",),
        merged_summaries=("xAI 發布 Grok 4.7。",),
    )

    kept = dedupe_entries([listed, covered])

    assert len(kept) == 1
    assert kept[0].source_name == "AlphaSignal"


HN_DISCUSSION = "https://news.ycombinator.com/item?id=123"


@pytest.mark.parametrize(
    ("article_url", "content_basis", "also_from", "expected"),
    [
        ("https://example.com/story", "newsletter", (), True),
        (None, "newsletter", (), False),
        (None, "newsletter", ("AlphaSignal",), True),
        (None, "hn_self_post", (), True),
        (None, "article", (), True),
        (None, "metadata", (), False),
        # A self-post whose body could not be read stores its discussion page as the article link.
        (HN_DISCUSSION, "metadata", (), False),
        (HN_DISCUSSION, "hn_self_post", (), True),
        ("https://example.com/story", "metadata", (), True),
    ],
)
def test_what_counts_as_something_behind_an_entry(
    article_url: str | None, content_basis: str, also_from: tuple[str, ...], expected: bool
) -> None:
    # An article to fetch, another newsletter's coverage, or a text the extractor read in full.
    story = replace(
        entry(1, "Story", "TLDR"),
        article_url=article_url,
        discussion_url=HN_DISCUSSION,
        content_basis=content_basis,
        also_from=also_from,
        merged_summaries=("另一份電子報的摘要",) if also_from else (),
    )

    assert has_source_text(story) is expected


def test_a_second_copy_from_the_same_newsletter_is_not_coverage() -> None:
    # Deduping folds the copy's summary into merged_summaries, but it is the same newsletter's text
    # again; left counting, the title would reach the reviewer and could become a headline.
    first = _headline_only(1, "Grok 4.7")
    copy = replace(_headline_only(2, "Grok 4.7"), item=first.item.model_copy(update={"summary_zh_tw": "另一段摘要"}))

    kept = dedupe_entries([first, copy])

    assert len(kept) == 1 and kept[0].merged_summaries
    assert not has_source_text(kept[0])


def _earlier(day: int, candidate_id: int, title: str, url: str | None = None) -> tuple[date, DigestEntry]:
    return date(2026, 9, day), replace(entry(candidate_id, title, "AlphaSignal"), article_url=url)


def test_the_same_article_link_marks_a_repeat_without_asking_the_model() -> None:
    def never_asked(_left: DigestEntry, _right: DigestEntry) -> bool:
        raise AssertionError("the link settles it")

    today = replace(entry(1, "Opus 5.5 pricing", "TLDR AI"), article_url="https://anthropic.com/opus?utm_source=x")

    marked = with_previous_coverage(
        [today], [_earlier(24, 100, "Anthropic releases Opus 5.5", "https://anthropic.com/opus")], never_asked, 3, []
    )

    assert (marked[0].previous_days, marked[0].previous_window) == (1, 3)


def test_a_repeat_without_a_shared_link_needs_a_shortlist_and_the_models_yes() -> None:
    today = entry(1, "Claude Opus 5.5 is 40% cheaper", "TLDR AI")
    earlier = [_earlier(23, 100, "Anthropic releases Claude Opus 5.5"), _earlier(24, 101, "Stripe knowledge platform")]

    agreed = with_previous_coverage([today], earlier, lambda _left, _right: True, 3, [])
    refused = with_previous_coverage([today], earlier, lambda _left, _right: False, 3, [])

    # The Opus items share "opus 5.5"; Stripe shares nothing, so only one day can count.
    assert agreed[0].previous_days == 1
    assert refused[0].previous_days == 0


def test_repeats_count_days_not_items_and_stop_at_the_window() -> None:
    today = entry(1, "GPT-6 Sol and Luna", "TLDR AI")
    earlier = [
        _earlier(22, 100, "GPT-6 Sol"),
        _earlier(23, 101, "GPT-6 Sol launch"),
        _earlier(23, 102, "GPT-6 Sol pricing"),
        _earlier(24, 103, "GPT-6 Sol and Luna"),
        _earlier(25, 104, "GPT-6 Sol again"),
    ]

    asked: list[int | None] = []

    def judge(_left: DigestEntry, right: DigestEntry) -> bool:
        asked.append(right.candidate_id)
        return True

    marked = with_previous_coverage([today], earlier, judge, 3, [])

    assert (marked[0].previous_days, marked[0].previous_window) == (3, 3)
    # A day already counted is not asked about again: 102 shares 101's day.
    assert asked == [100, 101, 103, 104]


def test_the_repeat_mark_is_shown_on_headlines_and_mentions() -> None:
    headline = replace(entry(1, "Opus 5.5", "TLDR"), review_score=90, previous_days=2, previous_window=3)
    mention = replace(entry(2, "GPT-6 Sol", "TLDR"), previous_days=3, previous_window=3)
    fresh = entry(3, "Rune IDE", "TLDR")

    content = render_digest([headline, mention, fresh], datetime(2026, 9, 25), "AI", "TLDR", 1)

    top, rest = content.split("🧰")
    assert "   🔁 前 3 天中有 2 天也有報導" in top.splitlines()
    assert "• GPT-6 Sol · TLDR · 🔁 3/3 天 · <https://example.com/story-2>" in rest.splitlines()
    assert "Rune IDE · TLDR · <https://example.com/story-3>" in rest


def test_a_merge_keeps_the_longer_run_of_coverage() -> None:
    headline = replace(entry(1, "Opus 5.5", "TLDR"), review_score=90, previous_days=1, previous_window=3)
    copy = replace(entry(2, "Opus 5.5 pricing", "AlphaSignal"), previous_days=3, previous_window=3)

    merged, _ = merge_related_entries([headline], [copy], lambda _left, _right: True)

    assert merged[0].previous_days == 3


def test_the_reviewer_is_told_how_many_previous_days_carried_a_story() -> None:
    seen: list[dict[str, object]] = []

    class FakeOllama:
        def review_digest(self, candidates: list[dict[str, object]], maximum: int, *_: object) -> DigestReview:
            seen.extend(candidates)
            return DigestReview.model_validate({"selected": [{"candidate_id": 1, "score": 90, "reason_zh_tw": "具體"}]})

    ranked = [replace(entry(1, "Opus 5.5", "TLDR"), previous_days=2, previous_window=3), entry(2, "Rune IDE", "TLDR")]

    pipeline._reviewed_entries(Settings(digest_max_items=1), FakeOllama(), ranked)

    assert seen[0]["previous_days"] == 2
    assert "previous_days" not in seen[1]


def test_previous_coverage_comes_from_earlier_runs_within_the_window(tmp_path: Path) -> None:
    # Received early three days ago, yesterday and ten days ago, plus a failed one two days ago,
    # another run's document earlier today, and this run's own. Only the first two may count: the
    # failed one has nothing to show, earlier today is not a previous day however many runs it
    # took, and the story is never taken as repeating itself.
    from two_much_two_read.schemas import DigestItem as StoredItem
    from two_much_two_read.storage import Database

    database = Database(tmp_path / "digest.sqlite3")
    documents: dict[str, int] = {}
    for name, received in (
        # 06:00 in Montreal, before this run's hour: inside whole calendar days, outside 72 hours.
        ("three-days-early", datetime(2026, 9, 22, 10, tzinfo=UTC)),
        ("failed", datetime(2026, 9, 23, 14, tzinfo=UTC)),
        ("yesterday", datetime(2026, 9, 24, 14, tzinfo=UTC)),
        ("ten-days", datetime(2026, 9, 15, 14, tzinfo=UTC)),
        # 01:00 in Montreal, stored by a source-specific run before this one.
        ("earlier-today", datetime(2026, 9, 25, 5, tzinfo=UTC)),
        ("this-run", datetime(2026, 9, 25, 11, tzinfo=UTC)),
    ):
        document_id = database.discover_gmail_document(name, name, "tldr-ai", received, "subject", "sender", "body", False)
        assert document_id is not None
        database.store_items(
            document_id,
            [
                StoredItem(
                    title="GPT-6 Sol launches",
                    category="AI_MODEL",
                    summary_zh_tw="摘要",
                    why_it_matters_zh_tw="原因",
                    importance=5,
                    confidence=0.5,
                )
            ],
        )
        documents[name] = document_id
    database.fail_document(documents["failed"], "X")
    judged: list[str] = []

    class Judge:
        def same_story(self, left: dict[str, str], right: dict[str, str]) -> bool:
            judged.append(right["title"])
            return True

    mark = pipeline._repeat_marker(
        Settings(digest_timezone="America/Montreal"),
        database,
        Judge(),
        datetime(2026, 9, 25, 8, tzinfo=ZoneInfo("America/Montreal")),
        [documents["this-run"]],
        {"tldr-ai": "TLDR AI"},
        lambda _message: None,
    )
    marked = mark([entry(documents["this-run"] * 1000, "GPT-6 Sol and Luna", "TLDR AI")], lambda _entry: True)
    database.close()

    assert (marked[0].previous_days, marked[0].previous_window) == (2, 3)
    assert len(judged) == 2


def test_a_zero_window_turns_the_mark_off(tmp_path: Path) -> None:
    from two_much_two_read.storage import Database

    class Unused:
        def same_story(self, *_: object) -> bool:
            raise AssertionError("nothing to compare against")

    ranked = [entry(1, "Opus 5.5", "TLDR")]
    database = Database(tmp_path / "digest.sqlite3")
    try:
        mark = pipeline._repeat_marker(
            Settings(digest_repeat_window_days=0), database, Unused(), datetime.now(UTC), [], {}, lambda _message: None
        )
        marked = mark(ranked, lambda _entry: True)
    finally:
        database.close()

    assert marked == ranked


def test_without_a_versioned_name_only_rare_shared_tokens_shortlist_a_pair() -> None:
    # "Meta" and "Muse" are each in few of the background items, so together they are worth asking
    # about; "AI" is in most of them and is worth nothing.
    background = [{"ai"}] * 300 + [{"meta"}] * 6 + [{"muse"}] * 4
    asked: list[int | None] = []

    def judge(_left: DigestEntry, right: DigestEntry) -> bool:
        asked.append(right.candidate_id)
        return True

    today = entry(1, "Meta Muse AI avatars", "TLDR AI")
    earlier = [_earlier(23, 100, "Meta Muse assistant"), _earlier(24, 101, "AI roundup")]

    marked = with_previous_coverage([today], earlier, judge, 3, background)

    assert asked == [100]
    assert marked[0].previous_days == 1


def test_a_count_absorbed_in_merging_is_not_lowered_by_a_later_check() -> None:
    # Checked after merging, a mention may already carry three days from the copy it absorbed.
    today = replace(entry(1, "Claude Opus 5.5", "TLDR"), previous_days=3, previous_window=3)

    marked = with_previous_coverage([today], [_earlier(24, 100, "Anthropic releases Opus 5.5")], lambda *_: True, 3, [])

    assert marked[0].previous_days == 3


def test_a_day_is_asked_about_in_turn_until_one_earlier_item_agrees() -> None:
    # The best-scoring match on a day can be a roundup of several stories, rightly judged different.
    today = entry(1, "Claude Opus 5.5", "TLDR")
    roundup = _earlier(23, 100, "Opus 5.5, GPT-6 Sol, GPT-6 Luna and a price war")
    single = _earlier(23, 101, "Anthropic releases Opus 5.5")
    asked: list[int | None] = []

    def judge(_left: DigestEntry, right: DigestEntry) -> bool:
        asked.append(right.candidate_id)
        return right.candidate_id == 101

    marked = with_previous_coverage([today], [roundup, single], judge, 3, [])

    assert asked == [100, 101]
    assert marked[0].previous_days == 1


def test_repeats_are_checked_on_what_the_reviewer_and_the_reader_see(tmp_path: Path) -> None:
    # One reviewer slot and one mention slot. Before the review only the reviewer's candidate is
    # checked. The first bare mention folds into that headline, which frees its slot for the second -
    # a mention that could not be known before merging, and still repeats yesterday's story. The
    # headline is not judged twice, and the mention folded away is never judged at all.
    from two_much_two_read.schemas import DigestItem as StoredItem
    from two_much_two_read.storage import Database

    database = Database(tmp_path / "digest.sqlite3")
    yesterday = database.discover_gmail_document("y", "y", "tldr-ai", datetime(2026, 9, 24, 14, tzinfo=UTC), "s", "f", "b", False)
    assert yesterday is not None
    database.store_items(
        yesterday,
        [
            StoredItem(
                title="Grok 4.7 launch",
                category="AI_MODEL",
                summary_zh_tw="摘要",
                why_it_matters_zh_tw="原因",
                importance=5,
                confidence=0.5,
            )
        ],
    )
    asked: list[str] = []

    class RepeatJudge:
        def same_story(self, left: dict[str, str], right: dict[str, str]) -> bool:
            asked.append(left["title"])
            return True

    class Reviewer:
        def review_digest(self, candidates: list[dict[str, object]], maximum: int, *_: object) -> DigestReview:
            return DigestReview.model_validate({"selected": [{"candidate_id": 1, "score": 90, "reason_zh_tw": "具體"}]})

        def same_story(self, left: dict[str, str], right: dict[str, str]) -> bool:
            return left["title"] == right["title"]

    settings = Settings(digest_review_candidate_limit=1, digest_secondary_items=1, digest_timezone="America/Montreal")
    messages: list[str] = []
    mark = pipeline._repeat_marker(
        settings,
        database,
        RepeatJudge(),
        datetime(2026, 9, 25, 8, tzinfo=ZoneInfo("America/Montreal")),
        [],
        {},
        messages.append,
    )
    ranked = [
        entry(1, "Grok 4.7 pricing", "TLDR AI"),
        _headline_only(2, "Grok 4.7 pricing"),
        _headline_only(3, "Grok 4.7 benchmarks"),
    ]
    shown, _ = pipeline._selected_entries(settings, Reviewer(), ranked, mark, lambda _message: None)
    database.close()

    assert [value.item.title for value in shown] == ["Grok 4.7 pricing", "Grok 4.7 benchmarks"]
    assert [value.previous_days for value in shown] == [1, 1]
    assert asked == ["Grok 4.7 pricing", "Grok 4.7 benchmarks"]
    assert messages == ["Checking 1 candidates against 1 items from the previous 3 days"] * 2


def _headline(candidate_id: int, source: str, score: int) -> DigestEntry:
    return replace(entry(candidate_id, f"Story {candidate_id}", source), review_score=score, reranker_score=score / 100)


def test_one_newsletter_holds_at_most_its_share_of_the_headlines() -> None:
    # Console's tool list took three of ten headlines on 2026-09-26. Its best two stay; the third is a
    # mention, and the reviewer's next pick from elsewhere takes the slot.
    picks = [
        _headline(1, "Console", 95),
        _headline(2, "Console", 94),
        _headline(3, "Console", 93),
        _headline(4, "TLDR", 92),
        _headline(5, "SANS", 60),
    ]

    capped, _ = pipeline._merged_entries(picks, 10, never_the_same, headline_limit=3, per_source=2)
    uncapped, _ = pipeline._merged_entries(picks, 10, never_the_same, headline_limit=3)

    assert [(value.candidate_id, value.review_score is not None) for value in capped] == [
        (1, True),
        (2, True),
        (4, True),
        (3, False),
        (5, False),
    ]
    assert [value.candidate_id for value in uncapped if value.review_score is not None] == [1, 2, 3, 4, 5]


def test_a_pick_the_cap_leaves_out_competes_for_the_mention_quota_like_any_mention() -> None:
    picks = [_headline(1, "Console", 95), _headline(2, "Console", 94), _headline(3, "Console", 93)]
    passed_over = [replace(entry(4, "Other story", "TLDR"), reranker_score=0.99)]

    merged, _ = pipeline._merged_entries([*picks, *passed_over], 1, never_the_same, headline_limit=5, per_source=2)

    # One mention slot: the passed-over candidate ranks above the capped pick, so it is the one shown.
    assert [value.candidate_id for value in merged] == [1, 2, 4]


def test_the_reviewer_is_asked_for_picks_past_the_limit_only_while_the_cap_is_on() -> None:
    asked: list[int] = []

    class FakeOllama:
        def review_digest(self, candidates: list[dict[str, object]], maximum: int, *_: object) -> DigestReview:
            asked.append(maximum)
            return DigestReview.model_validate({"selected": []})

    ranked = [entry(1, "Opus 5.5", "TLDR")]
    pipeline._reviewed_entries(Settings(digest_max_items=10), FakeOllama(), ranked)
    pipeline._reviewed_entries(Settings(digest_max_items=10, digest_headlines_per_source=0), FakeOllama(), ranked)

    assert asked == [10 + pipeline.REVIEW_REFILL_PICKS, 10]
