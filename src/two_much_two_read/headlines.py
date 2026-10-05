"""Finishing the selected entries: checking each shown title, then rewriting the headlines from their sources."""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

import httpx

from .article_extractor import ArticleExtractionError, extract_article
from .article_fetcher import ArticleFetcher, ArticleFetchError, UrlResolutionError
from .config import Settings
from .digest import (
    DigestEntry,
    canonical_url,
)
from .ollama import (
    OllamaContextError,
    OllamaSchemaError,
    checked_title,
    source_headline,
    title_from_summary,
    translated_from,
)
from .schemas import (
    HeadlineCheck,
    ItemDeepening,
)
from .stages import StatusReporter, _unload_model


class DeepensItems(Protocol):
    def deepen_item(self, title: str, category: str, basis: str, content: str) -> ItemDeepening: ...


class ChecksTitles(Protocol):
    def back_translated(self, title: str, source_id: str = ...) -> str | None: ...
    def translated_headline(self, headline: str, source_id: str = ...) -> str | None: ...
    def headline_supported(self, headline: str, back: str) -> HeadlineCheck | None: ...
    def unload(self, model: str) -> bool: ...


def _article_to_deepen_from(entry: DigestEntry) -> str | None:
    """The story's own article, or nothing - never the page the discussion lives on.

    A Hacker News self-post has no article, so its stored source_url falls back to the discussion
    URL. Fetching that returns the whole thread, and extract_article cannot tell the author's post
    from the replies to it: the rewrite would restate a commenter's claim as the headline's own
    finding. The extractor already read the self-post text when the item was analysed, so the
    newsletter fallback below is that text's summary rather than a loss.
    """
    url = entry.article_url or (str(entry.item.source_url) if entry.item.source_url else None)
    if url and canonical_url(url) == canonical_url(entry.discussion_url):
        return None
    return url


def _headline_source(entry: DigestEntry, fetcher: ArticleFetcher, status: StatusReporter) -> tuple[str, str]:
    """The fullest text available for a headline: the article itself, or the newsletters on it.

    The extractor's own summary is the floor, not the goal - it was written from a few lines of one
    email. Email bodies are never persisted, so the article is the only route to more text, and the
    merged newsletter coverage is the fallback when there is no link or the fetch fails.
    """
    url = _article_to_deepen_from(entry)
    if url:
        try:
            fetched = fetcher.fetch(url)
            return extract_article(fetched.content_type, fetched.body).text, "article"
        except (ArticleFetchError, ArticleExtractionError, UrlResolutionError):
            pass
        except Exception as error:
            # Deliberately broad, and reported rather than swallowed. This parses third-party HTML
            # that nothing in this project controls, and a longer summary is an enhancement: no
            # shape of page should be able to end a run that has already extracted, ranked, and
            # reviewed a full digest. One did - a hidden ancestor with a styled descendant raised a
            # TypeError out of the parser and took the whole digest with it. Anything reaching here
            # is a defect rather than an unreachable page, so it is named: a silent fallback would
            # let the rewrite quietly stop working for a whole class of pages.
            status(f"Warning: could not read {url} ({type(error).__name__}); using the newsletter summaries")
    summaries = entry.merged_summaries or (entry.item.summary_zh_tw,)
    return "\n\n".join((*summaries, entry.item.why_it_matters_zh_tw)), "newsletters"


def _checked_titles(
    settings: Settings, ollama: ChecksTitles, entries: list[DigestEntry], status: StatusReporter
) -> list[DigestEntry]:
    """The shown entries with each translated title checked against the newsletter's own headline.

    A title is read back into English by the translation model, which never sees the headline, and
    the review model compares the two; see HEADLINE_CHECK_SYSTEM_PROMPT. A title that fails, or that
    a rule can see is wrong (cut short, or a year the headline never gave), is replaced by the
    translation model's own translation of the headline, which passed the same rules. Without one, a
    title a rule ruled out takes the summary's lead and a flagged one stays, logged: about three
    flags in four are sound titles, so a flag is reason to swap a title for a plainer one, never to
    lose an entry or move it.

    The translation model runs for every title first and the review model after, so each loads once.
    """
    if not settings.digest_check_titles:
        return entries
    language = settings.digest_language
    headlines: dict[int, str] = {}
    for index, entry in enumerate(entries):
        headline = source_headline(entry.item.source_title)
        if headline is not None and translated_from(headline, entry.item.title, language):
            headlines[index] = headline
    if not headlines:
        return entries
    # The review model is still loaded from selection, and both do not fit beside each other.
    _unload_model(ollama, settings.ollama_review_model, status)
    readings: dict[int, tuple[str | None, str | None, str | None]] = {}
    for index, headline in headlines.items():
        source_id = entries[index].source_id or "digest"
        title = entries[index].item.title
        back = None if checked_title(headline, title, language) is None else ollama.back_translated(title, source_id)
        alternative = ollama.translated_headline(headline, source_id)
        alternative_back = None if alternative is None else ollama.back_translated(alternative, source_id)
        readings[index] = (back, alternative, alternative_back)
    _unload_model(ollama, settings.ollama_translate_model, status)

    def verdict(headline: str, back: str | None) -> HeadlineCheck | None:
        return None if back is None else ollama.headline_supported(headline, back)

    checked: list[DigestEntry] = []
    for index, entry in enumerate(entries):
        if index not in readings:
            checked.append(entry)
            continue
        headline, title = headlines[index], entry.item.title
        back, alternative, alternative_back = readings[index]
        ruled_out = checked_title(headline, title, language) is None
        check = None if ruled_out else verdict(headline, back)
        # A check that could not run is no evidence against the title.
        if not ruled_out and (check is None or check.supported):
            checked.append(entry)
            continue
        reason = "fails the title rules" if check is None else check.reason
        # The replacement is held to the same check, and needs it to pass: it is another model's
        # reading of the headline, and one that could not be checked is no better than the title.
        if alternative is not None and not (
            (replacement_check := verdict(headline, alternative_back)) and replacement_check.supported
        ):
            alternative = None
        replacement = alternative or (title_from_summary(entry.item.summary_zh_tw) if ruled_out else None)
        if replacement is None:
            status(f"Title check: kept {title!r} for {headline!r}, with nothing to replace it ({reason[:160]})")
            checked.append(entry)
            continue
        status(f"Title check: {title!r} -> {replacement!r} for {headline!r} ({reason[:160]})")
        checked.append(replace(entry, item=entry.item.model_copy(update={"title": replacement})))
    return checked


def _deepened_entries(
    settings: Settings, ollama: DeepensItems, entries: list[DigestEntry], status: StatusReporter
) -> list[DigestEntry]:
    if not settings.digest_deepen_headlines:
        return entries
    fetcher = ArticleFetcher()
    deepened: list[DigestEntry] = []
    for entry in entries:
        if entry.review_score is None:
            deepened.append(entry)
            continue
        content, basis = _headline_source(entry, fetcher, status)
        if basis == "newsletters" and not entry.merged_summaries:
            # The fallback is this item's own summary, so there is nothing fuller to rewrite from.
            # The prompt asks for four to six sentences naming versions and numbers, and a single
            # 60-character summary supports none of that: the model could only pad or invent, and
            # the reader would have no way to tell an expanded headline from an inflated one. A
            # generation is skipped rather than spent turning one sentence into six.
            deepened.append(entry)
            continue
        status(f"Expanding {entry.item.title}")
        try:
            rewrite = ollama.deepen_item(entry.item.title, entry.item.category, basis, content)
        except (OllamaContextError, OllamaSchemaError, httpx.HTTPError) as error:
            # A headline with its original short summary still beats losing the digest.
            status(f"Warning: kept the original summary for {entry.item.title} ({type(error).__name__})")
            deepened.append(entry)
            continue
        if not rewrite.covers_the_item:
            status(f"Warning: {basis} source did not cover {entry.item.title}; kept the original summary")
            deepened.append(entry)
            continue
        item = entry.item.model_copy(
            update={"summary_zh_tw": rewrite.summary_zh_tw, "why_it_matters_zh_tw": rewrite.why_it_matters_zh_tw}
        )
        deepened.append(replace(entry, item=item))
    return deepened
