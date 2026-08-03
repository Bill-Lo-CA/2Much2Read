import base64
from email.message import EmailMessage

import pytest

from two_much_two_read.mime import EmailExtractionError, EmptyEmailError, extract_gmail_payload, extract_mime, html_to_text


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
    import two_much_two_read.mime as mime

    monkeypatch.setattr(mime.BytesParser, "parsebytes", lambda *args, **kwargs: pytest.fail("parser should not run"))

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
    payload = {
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

    payload = {
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


def test_extract_gmail_payload_counts_duplicate_links_toward_limit() -> None:
    html = "".join('<a href="https://example.com/article">article</a>' for _ in range(1_000))
    payload = {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()}}

    with pytest.raises(EmailExtractionError) as error:
        extract_gmail_payload(payload)

    assert error.value.code == "EMAIL_TOO_MANY_LINKS"


@pytest.mark.parametrize(("length", "truncated"), [(45_000, False), (45_001, True)])
def test_extract_gmail_payload_preserves_original_length_and_caps_analysis_text(length: int, truncated: bool) -> None:
    text = "x" * length
    payload = {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}

    content = extract_gmail_payload(payload)

    assert len(content.analysis_text) == min(length, 45_000)
    assert content.original_characters == length
    assert ((content.original_characters or 0) > 45_000) is truncated


def test_gmail_payload_skips_malformed_part_and_uses_valid_text() -> None:
    payload = {
        "parts": [
            {"mimeType": "text/plain", "body": {"data": "%%%"}},
            {"mimeType": "text/plain", "body": {"data": "dmFsaWQ"}},
        ]
    }

    assert extract_gmail_payload(payload).analysis_text == "valid"
