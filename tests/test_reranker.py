from __future__ import annotations

import hashlib
import sys
import types
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

import pytest

from two_much_two_read import pipeline
from two_much_two_read.command_models import SecurityFloorPromotion
from two_much_two_read.config import Settings
from two_much_two_read.digest import DigestEntry, dedupe_entries, has_source_text, merge_related_entries, render_digest
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
    settings = Settings(digest_max_items=1, digest_review_candidate_limit=2, ollama_review_model="qwen3:8b")

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


def test_the_copy_with_an_article_is_kept_when_a_bare_one_repeats_it() -> None:
    # Hacker Newsletter's "Grok 4.7" ranked above AlphaSignal's write-up of the same launch; kept as
    # the primary, its one guessed line would have replaced the real summary and its article.
    listed = replace(_headline_only(1, "Grok 4.7"), reranker_score=0.9)
    written = replace(entry(2, "xAI releases Grok 4.7", "AlphaSignal"), reranker_score=0.5)

    _, mentions = merge_related_entries([], [listed, written], lambda _left, _right: True)

    assert len(mentions) == 1
    assert (mentions[0].item.title, mentions[0].also_from) == ("xAI releases Grok 4.7", ("Hacker Newsletter",))


def test_an_identical_bare_copy_loses_to_the_one_with_coverage_behind_it() -> None:
    # Same title and no article on either side, so dedupe_entries keys them together; the one other
    # newsletters also covered is kept, whatever the reranker thought of the two.
    listed = replace(_headline_only(1, "Grok 4.7"), reranker_score=0.9)
    covered = replace(
        _headline_only(2, "Grok 4.7"), source_name="AlphaSignal", reranker_score=0.5, merged_summaries=("xAI 發布 Grok 4.7。",)
    )

    kept = dedupe_entries([listed, covered])

    assert len(kept) == 1
    assert kept[0].source_name == "AlphaSignal"


@pytest.mark.parametrize(
    ("article_url", "content_basis", "merged_summaries", "expected"),
    [
        ("https://example.com/story", "newsletter", (), True),
        (None, "newsletter", (), False),
        (None, "newsletter", ("另一份電子報的摘要",), True),
        (None, "hn_self_post", (), True),
        (None, "article", (), True),
        (None, "metadata", (), False),
    ],
)
def test_what_counts_as_something_behind_an_entry(
    article_url: str | None, content_basis: str, merged_summaries: tuple[str, ...], expected: bool
) -> None:
    # An article to fetch, another newsletter's coverage, or a text the extractor read in full.
    story = replace(
        entry(1, "Story", "TLDR"), article_url=article_url, content_basis=content_basis, merged_summaries=merged_summaries
    )

    assert has_source_text(story) is expected
