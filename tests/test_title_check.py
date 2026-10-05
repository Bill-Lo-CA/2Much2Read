from __future__ import annotations

from two_much_two_read import pipeline
from two_much_two_read.config import Settings
from two_much_two_read.digest import DigestEntry
from two_much_two_read.schemas import DigestItem, HeadlineCheck

HEADLINE = "OpenAI blocks distillation attack"


def entry(title: str, source_title: str | None = HEADLINE, review_score: int | None = 90) -> DigestEntry:
    return DigestEntry(
        DigestItem(
            title=title,
            source_title=source_title,
            category="SECURITY",
            summary_zh_tw="摘要。",
            why_it_matters_zh_tw="原因。",
            importance=8,
            confidence=0.8,
        ),
        source_id="risky-business-news",
        source_name="Risky Business News",
        review_score=review_score,
    )


class FakeTitles:
    """Back-translations and verdicts by text; records each call in order, unloads included."""

    def __init__(self, backs: dict[str, str], translations: dict[str, str], rejected: set[str]) -> None:
        self.backs = backs
        self.translations = translations
        self.rejected = rejected
        self.calls: list[str] = []

    def back_translated(self, title: str, source_id: str = "digest") -> str | None:
        self.calls.append(f"back {title}")
        return self.backs.get(title)

    def translated_headline(self, headline: str, source_id: str = "digest") -> str | None:
        self.calls.append(f"translate {headline}")
        return self.translations.get(headline)

    def headline_supported(self, headline: str, back: str) -> HeadlineCheck | None:
        self.calls.append(f"check {back}")
        return HeadlineCheck(supported=back not in self.rejected, reason=f"{back} is not {headline}")

    def unload(self, model: str) -> bool:
        self.calls.append(f"unload {model}")
        return True


def fake(rejected: frozenset[str] = frozenset(), translation: str | None = "OpenAI 阻止蒸餾攻擊") -> FakeTitles:
    return FakeTitles(
        {
            "OpenAI 擴散模型攻擊被阻": "OpenAI's diffusion model attacks blocked",
            "OpenAI 阻止蒸餾攻擊": "OpenAI blocks distillation attacks",
        },
        {HEADLINE: translation} if translation else {},
        set(rejected),
    )


def test_a_title_that_says_what_the_headline_does_is_kept() -> None:
    ollama = fake()
    shown = [entry("OpenAI 阻止蒸餾攻擊")]

    assert pipeline._checked_titles(Settings(), ollama, shown, lambda _: None) == shown


def test_a_title_read_back_as_another_fact_gives_way_to_the_translators() -> None:
    """The 2026-10-02 headline. It keeps its place: most flags are sound titles, so a flag costs only the title."""
    ollama = fake(rejected=frozenset({"OpenAI's diffusion model attacks blocked"}))
    statuses: list[str] = []

    checked = pipeline._checked_titles(Settings(), ollama, [entry("OpenAI 擴散模型攻擊被阻")], statuses.append)

    assert [(value.item.title, value.review_score) for value in checked] == [("OpenAI 阻止蒸餾攻擊", 90)]
    assert "OpenAI 擴散模型攻擊被阻" in statuses[0] and "diffusion" in statuses[0]


def test_a_flagged_title_with_nothing_to_replace_it_stays_and_is_logged() -> None:
    """Most flags are sound titles; without a translation of the headline, losing the entry costs more."""
    ollama = fake(rejected=frozenset({"OpenAI's diffusion model attacks blocked"}), translation=None)
    statuses: list[str] = []
    shown = [entry("OpenAI 擴散模型攻擊被阻")]

    assert pipeline._checked_titles(Settings(), ollama, shown, statuses.append) == shown
    assert "kept" in statuses[0] and "diffusion" in statuses[0]


def test_a_title_cut_short_is_replaced_without_asking_either_model_about_it() -> None:
    """GPT-...（需要翻譯） was shown on 2026-10-02; the review model had called its kind supported."""
    ollama = fake()

    checked = pipeline._checked_titles(Settings(), ollama, [entry("GPT-...（需要翻譯）")], lambda _: None)

    assert [value.item.title for value in checked] == ["OpenAI 阻止蒸餾攻擊"]
    assert not any(call.startswith(("back", "check")) for call in ollama.calls)


def test_a_title_cut_short_with_no_translation_takes_the_summarys_lead() -> None:
    ollama = fake(translation=None)

    checked = pipeline._checked_titles(Settings(), ollama, [entry("GPT-...（需要翻譯）")], lambda _: None)

    assert [value.item.title for value in checked] == ["摘要"]


def test_a_check_that_cannot_run_keeps_the_title() -> None:
    class Silent(FakeTitles):
        def headline_supported(self, headline: str, back: str) -> HeadlineCheck | None:
            return None

    ollama = Silent({"OpenAI 擴散模型攻擊被阻": "OpenAI's diffusion model attacks blocked"}, {}, set())
    shown = [entry("OpenAI 擴散模型攻擊被阻")]

    assert pipeline._checked_titles(Settings(), ollama, shown, lambda _: None) == shown


def test_each_model_loads_once_with_the_review_model_out_of_the_way() -> None:
    settings = Settings()
    ollama = fake()

    pipeline._checked_titles(settings, ollama, [entry("OpenAI 阻止蒸餾攻擊"), entry("OpenAI 阻止蒸餾攻擊")], lambda _: None)

    unload_review, unload_translator = (
        ollama.calls.index(f"unload {settings.ollama_review_model}"),
        ollama.calls.index(f"unload {settings.ollama_translate_model}"),
    )
    translator = [index for index, call in enumerate(ollama.calls) if call.startswith(("back", "translate"))]
    checks = [index for index, call in enumerate(ollama.calls) if call.startswith("check")]
    assert unload_review < min(translator) and max(translator) < unload_translator < min(checks)


def test_nothing_runs_for_titles_that_were_not_translated_or_have_no_headline() -> None:
    ollama = fake()
    shown = [
        entry("OpenAI blocks distillation attack"),  # left in English
        entry("OpenAI 阻止蒸餾攻擊", source_title=None),  # stored before headlines were kept
        entry("OpenAI 阻止蒸餾攻擊", source_title="View this post on the web at https://example.com"),
    ]

    assert pipeline._checked_titles(Settings(), ollama, shown, lambda _: None) == shown
    assert ollama.calls == []


def test_the_check_can_be_turned_off() -> None:
    ollama = fake(rejected=frozenset({"OpenAI's diffusion model attacks blocked"}))
    shown = [entry("OpenAI 擴散模型攻擊被阻")]

    assert pipeline._checked_titles(Settings(digest_check_titles=False), ollama, shown, lambda _: None) == shown
    assert ollama.calls == []
