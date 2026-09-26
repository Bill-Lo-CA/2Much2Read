import base64
from email.message import EmailMessage

import pytest
from bs4.element import Tag

from two_much_two_read.mime import (
    MAX_LINK_CANDIDATES,
    MAX_LINK_OCCURRENCES,
    EmailExtractionError,
    EmptyEmailError,
    extract_gmail_payload,
    extract_mime,
    html_to_text,
)
from two_much_two_read.schemas import ExtractedEmailContent


@pytest.mark.parametrize(("plain", "html"), [("plain wins", None), ("plain wins", "<p>html loses</p>")])
def test_extract_mime_returns_plain_text_for_supported_structures(plain: str, html: str | None) -> None:
    message = EmailMessage()
    message.set_content(plain)
    if html is not None:
        message.add_alternative(html, subtype="html")

    assert extract_mime(message.as_bytes()).analysis_text == plain


@pytest.mark.parametrize(("length", "truncated"), [(45_000, False), (45_001, True)])
def test_extract_mime_preserves_original_length_and_caps_analysis_text(length: int, truncated: bool) -> None:
    text = "x" * length
    raw = b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + text.encode()

    content = extract_mime(raw)

    assert len(content.analysis_text) == min(length, 45_000)
    assert content.original_characters == length
    assert ((content.original_characters or 0) > 45_000) is truncated


def test_extract_mime_keeps_html_link_candidates_when_plain_text_wins() -> None:
    message = EmailMessage()
    message.set_content("plain summary")
    message.add_alternative(
        '<h2>Useful article</h2><a href="https://example.com/article?utm_source=newsletter">Useful article</a>',
        subtype="html",
    )

    content = extract_mime(message.as_bytes())

    assert content.analysis_text == "plain summary"
    assert [(candidate.candidate_id, str(candidate.raw_url), candidate.anchor_text) for candidate in content.link_candidates] == [
        ("link-0001", "https://example.com/article?utm_source=newsletter", "Useful article")
    ]


def test_extract_mime_keeps_plain_text_link_candidates_without_html() -> None:
    message = EmailMessage()
    message.set_content("Useful article: https://example.com/article")

    content = extract_mime(message.as_bytes())

    assert [(str(candidate.raw_url), candidate.nearby_text) for candidate in content.link_candidates] == [
        ("https://example.com/article", "Useful article")
    ]


def test_extract_mime_strips_plain_text_url_sentence_punctuation() -> None:
    message = EmailMessage()
    message.set_content("Read https://example.com/article?tags=research,security.")

    content = extract_mime(message.as_bytes())

    assert [(str(candidate.raw_url), candidate.nearby_text) for candidate in content.link_candidates] == [
        ("https://example.com/article?tags=research,security", "Read")
    ]


def test_extract_mime_keeps_balanced_parentheses_in_plain_text_urls() -> None:
    message = EmailMessage()
    message.set_content("Read https://en.wikipedia.org/wiki/Function_(mathematics).")

    content = extract_mime(message.as_bytes())

    assert [(str(candidate.raw_url), candidate.nearby_text) for candidate in content.link_candidates] == [
        ("https://en.wikipedia.org/wiki/Function_(mathematics)", "Read")
    ]


def test_extract_mime_prefers_html_context_when_plain_and_html_share_a_link() -> None:
    message = EmailMessage()
    message.set_content("Read [plain label](https://example.com/article)")
    message.add_alternative('<h2>Useful article</h2><a href="https://example.com/article">Useful article</a>', subtype="html")

    content = extract_mime(message.as_bytes())

    assert [(str(candidate.raw_url), candidate.anchor_text) for candidate in content.link_candidates] == [
        ("https://example.com/article", "Useful article")
    ]


def test_html_candidates_exclude_footer_and_unsafe_links() -> None:
    message = EmailMessage()
    message.set_content("plain summary")
    message.add_alternative(
        """<a href="https://example.com/article">Article</a>
        <a href="https://example.com/unsubscribe">Unsubscribe</a>
        <a href="javascript:alert(1)">Bad</a>""",
        subtype="html",
    )

    assert [str(candidate.raw_url) for candidate in extract_mime(message.as_bytes()).link_candidates] == [
        "https://example.com/article"
    ]


def test_extract_mime_skips_malformed_url_candidates() -> None:
    message = EmailMessage()
    message.set_content("plain summary")
    message.add_alternative(
        '<a href="https://example.com:bad/article">Broken link</a><a href="https://example.com/article">Useful article</a>',
        subtype="html",
    )

    assert [str(candidate.raw_url) for candidate in extract_mime(message.as_bytes()).link_candidates] == [
        "https://example.com/article"
    ]


def test_html_candidates_keep_articles_about_navigation_topics() -> None:
    message = EmailMessage()
    message.set_content("plain summary")
    message.add_alternative(
        '<h2>Account security and privacy policy update</h2><a href="https://example.com/privacy-policy-update">Read article</a>',
        subtype="html",
    )

    assert [str(candidate.raw_url) for candidate in extract_mime(message.as_bytes()).link_candidates] == [
        "https://example.com/privacy-policy-update"
    ]


def test_html_preserves_safe_links_and_drops_unsafe_ones() -> None:
    text = html_to_text(
        '<p>Read <a href="https://example.com/a">article</a></p><a href="javascript:alert(1)">bad</a><script>secret</script>'
    )
    assert "[article](https://example.com/a)" in text
    assert "javascript:" not in text
    assert "secret" not in text


def test_html_only_article_with_footer_words_is_not_truncated() -> None:
    text = html_to_text("<p>Google updated its privacy policy.</p><p>The change affects account controls.</p>")

    assert "Google updated its privacy policy." in text
    assert "The change affects account controls." in text


def test_html_footer_label_still_terminates_text() -> None:
    text = html_to_text("<p>Story</p><p>Unsubscribe | Privacy policy</p><p>Footer detail</p>")

    assert text == "Story"


def test_empty_email_fails() -> None:
    message = EmailMessage()
    message.set_content("")
    with pytest.raises(EmptyEmailError):
        extract_mime(message.as_bytes())


def test_empty_email_error_has_code() -> None:
    message = EmailMessage()
    message.set_content("")

    with pytest.raises(EmptyEmailError) as error:
        extract_mime(message.as_bytes())

    assert error.value.code == "EMAIL_NO_USABLE_TEXT"


def test_extract_mime_rejects_oversized_raw_input_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "two_much_two_read.mime.BytesParser.parsebytes", lambda *args, **kwargs: pytest.fail("parser should not run")
    )

    with pytest.raises(EmailExtractionError) as error:
        extract_mime(b"x" * (5 * 1024 * 1024 + 1))

    assert error.value.code == "EMAIL_TOO_LARGE"


def test_extract_mime_rejects_oversized_plain_bytes() -> None:
    message = EmailMessage()
    message.set_content("x" * (2 * 1024 * 1024 + 1))

    with pytest.raises(EmailExtractionError) as error:
        extract_mime(message.as_bytes())

    assert error.value.code == "EMAIL_TOO_LARGE"


def test_extract_mime_rejects_oversized_html_bytes() -> None:
    message = EmailMessage()
    message.add_alternative("<p>" + "x" * (2 * 1024 * 1024 + 1) + "</p>", subtype="html")

    with pytest.raises(EmailExtractionError) as error:
        extract_mime(message.as_bytes())

    assert error.value.code == "EMAIL_TOO_LARGE"


@pytest.mark.parametrize(("depth", "error_code"), [(20, None), (21, "EMAIL_STRUCTURE_TOO_COMPLEX")])
def test_extract_gmail_payload_enforces_depth_limit(depth: int, error_code: str | None) -> None:
    payload: dict[str, object] = {"mimeType": "multipart/mixed"}
    node = payload
    for _ in range(depth):
        child: dict[str, object] = {"mimeType": "multipart/mixed"}
        node["parts"] = [child]
        node = child
    node.update({"mimeType": "text/plain", "body": {"data": "eA"}})

    if error_code is None:
        assert extract_gmail_payload(payload).analysis_text == "x"
        return

    with pytest.raises(EmailExtractionError) as error:
        extract_gmail_payload(payload)
    assert error.value.code == error_code


@pytest.mark.parametrize(("child_count", "error_code"), [(199, None), (200, "EMAIL_STRUCTURE_TOO_COMPLEX")])
def test_extract_gmail_payload_enforces_part_limit(child_count: int, error_code: str | None) -> None:
    payload: dict[str, object] = {
        "mimeType": "multipart/mixed",
        "parts": [{"mimeType": "text/plain", "body": {"data": "eA"}} for _ in range(child_count)],
    }

    if error_code is None:
        assert extract_gmail_payload(payload).analysis_text
        return

    with pytest.raises(EmailExtractionError) as error:
        extract_gmail_payload(payload)
    assert error.value.code == error_code


def test_extract_gmail_payload_rejects_total_decoded_bytes() -> None:
    def encoded(size: int) -> str:
        return base64.urlsafe_b64encode(b"x" * size).decode()

    payload: dict[str, object] = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": encoded(2 * 1024 * 1024)}},
            {"mimeType": "text/html", "body": {"data": encoded(2 * 1024 * 1024)}},
            {"mimeType": "application/octet-stream", "body": {"data": encoded(1 * 1024 * 1024 + 1)}},
        ],
    }

    with pytest.raises(EmailExtractionError) as error:
        extract_gmail_payload(payload)

    assert error.value.code == "EMAIL_TOO_LARGE"


def _body(mime_type: str, text: str) -> dict[str, object]:
    return {"mimeType": mime_type, "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}


def _stories(count: int) -> list[str]:
    return [f"https://example.com/story-{number}" for number in range(count)]


def test_a_newsletter_that_repeats_its_links_in_both_parts_keeps_them_all() -> None:
    # The shape AINews, SemiAnalysis and Risky Bulletin arrive in: each story linked once in the
    # HTML and again in the plain part. 150 stories are 300 occurrences, and counting occurrences
    # against the 200 cap failed the whole email.
    urls = _stories(150)
    html = "".join(f'<p><a href="{url}">Story {number}</a></p>' for number, url in enumerate(urls))
    plain = "\n".join(f"Story {number} {url}" for number, url in enumerate(urls))
    payload: dict[str, object] = {
        "mimeType": "multipart/alternative",
        "parts": [_body("text/plain", plain), _body("text/html", html)],
    }

    content = extract_gmail_payload(payload)

    assert [str(candidate.raw_url) for candidate in content.link_candidates] == urls


def test_more_distinct_links_than_the_cap_keeps_the_first_ones_in_order() -> None:
    urls = _stories(500)
    html = "".join(f'<a href="{url}">Story {number}</a>' for number, url in enumerate(urls))

    content = extract_gmail_payload(_body("text/html", html))

    assert [str(candidate.raw_url) for candidate in content.link_candidates] == urls[:MAX_LINK_CANDIDATES]


def test_repeats_still_count_toward_the_work_bound() -> None:
    # The cap on kept links alone would let a message of endless copies of one anchor be scanned to
    # the end. A distinct link placed just past the bound shows where the scan stopped.
    repeated = '<a href="https://example.com/article">article</a>' * MAX_LINK_OCCURRENCES
    html = repeated + '<a href="https://example.com/past-the-bound">late</a>'

    content = extract_gmail_payload(_body("text/html", html))

    assert [str(candidate.raw_url) for candidate in content.link_candidates] == ["https://example.com/article"]


def test_only_kept_links_pay_for_their_surrounding_text(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding a link's heading walks back through the document, so paying for it on every repeat
    # would make the work bound quadratic in practice.
    import two_much_two_read.mime as module

    calls: list[object] = []
    real = module._nearby_text

    def counting(anchor: Tag) -> str:
        calls.append(anchor)
        return real(anchor)

    monkeypatch.setattr(module, "_nearby_text", counting)
    html = '<h2>Top</h2><a href="https://example.com/article">article</a>' * 1_000

    content = extract_gmail_payload(_body("text/html", html))

    assert len(content.link_candidates) == 1
    assert len(calls) == 1


@pytest.mark.parametrize(("length", "truncated"), [(45_000, False), (45_001, True)])
def test_extract_gmail_payload_preserves_original_length_and_caps_analysis_text(length: int, truncated: bool) -> None:
    text = "x" * length
    payload: dict[str, object] = {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}

    content = extract_gmail_payload(payload)

    assert len(content.analysis_text) == min(length, 45_000)
    assert content.original_characters == length
    assert ((content.original_characters or 0) > 45_000) is truncated


def test_gmail_payload_skips_malformed_part_and_uses_valid_text() -> None:
    payload: dict[str, object] = {
        "parts": [
            {"mimeType": "text/plain", "body": {"data": "%%%"}},
            {"mimeType": "text/plain", "body": {"data": "dmFsaWQ"}},
        ]
    }

    assert extract_gmail_payload(payload).analysis_text == "valid"


def _codes(content: ExtractedEmailContent) -> dict[str, str]:
    return {candidate.code: str(candidate.raw_url) for candidate in content.link_candidates}


def test_the_model_reads_link_codes_instead_of_urls() -> None:
    # The candidate list keeps every URL; the text the model reads names each one by its code.
    plain = (
        "Grok 4.7 https://example.com/grok?utm_source=newsletter (comments: https://news.example/item?id=1)\n"
        "[Claude Opus 5.5](https://example.com/opus) is out. See https://example.com/grok?utm_source=newsletter again."
    )

    content = extract_gmail_payload(_body("text/plain", plain))

    codes = _codes(content)
    assert "https://" not in content.analysis_text
    grok = next(code for code, url in codes.items() if url.startswith("https://example.com/grok"))
    comments = next(code for code, url in codes.items() if url.startswith("https://news.example/"))
    opus = next(code for code, url in codes.items() if url == "https://example.com/opus")
    assert content.analysis_text.splitlines() == [
        f"Grok 4.7 [{grok}] (comments: [{comments}])",
        f"Claude Opus 5.5 [{opus}] is out. See [{grok}] again.",
    ]


def test_code_shaped_text_the_newsletter_wrote_cannot_pass_for_a_code() -> None:
    # "[L2]" here is a cache level. Left as it is, the model could take it for the second link and
    # the text fields would delete it; in parentheses it keeps its words and loses the shape.
    plain = "Cache levels [L2] and [l 3] explained https://example.com/cache\n[L4](https://example.com/l4) roadmap"

    content = extract_gmail_payload(_body("text/plain", plain))

    codes = {url: code for code, url in _codes(content).items()}
    assert content.analysis_text.splitlines() == [
        f"Cache levels (L2) and (l 3) explained [{codes['https://example.com/cache']}]",
        f"L4 [{codes['https://example.com/l4']}] roadmap",
    ]


def test_an_html_newsletter_is_coded_the_same_way() -> None:
    html = '<h2>Top story</h2><p><a href="https://example.com/story">Top story</a> and more.</p>'

    content = extract_gmail_payload(_body("text/html", html))

    assert content.analysis_text == "Top story\nTop story [L1]\nand more."
    assert _codes(content) == {"L1": "https://example.com/story"}


def test_a_link_that_is_not_a_candidate_is_dropped_from_the_text() -> None:
    # An unsubscribe link is never a candidate, so no code may point at it; its URL goes too.
    html = '<p><a href="https://example.com/story">Story</a> · <a href="https://example.com/unsub">Unsubscribe</a></p>'

    content = extract_gmail_payload(_body("text/html", html))

    assert "example.com/unsub" not in content.analysis_text
    assert "Unsubscribe [L" not in content.analysis_text
    assert _codes(content) == {"L1": "https://example.com/story"}


def test_urls_no_longer_spend_the_character_budget() -> None:
    # A tracking URL per story used to fill the 45,000-character cut before the stories did.
    tracking = "https://click.example/" + "x" * 400
    plain = "\n".join(f"Story {number} {tracking}{number}" for number in range(150))

    content = extract_gmail_payload(_body("text/plain", plain))

    assert content.original_characters is not None and content.original_characters < 3_000
    assert content.analysis_text.endswith("Story 149 [L150]")
