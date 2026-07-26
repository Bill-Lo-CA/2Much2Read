from email.message import EmailMessage

import pytest

from two_much_two_read.mime import EmptyEmailError, extract_gmail_payload, extract_mime, html_to_text


@pytest.mark.parametrize(("plain", "html"), [("plain wins", None), ("plain wins", "<p>html loses</p>")])
def test_extract_mime_returns_plain_text_for_supported_structures(plain: str, html: str | None) -> None:
    message = EmailMessage()
    message.set_content(plain)
    if html is not None:
        message.add_alternative(html, subtype="html")

    assert extract_mime(message.as_bytes()).analysis_text == plain


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


def test_html_preserves_safe_links_and_drops_unsafe_ones() -> None:
    text = html_to_text(
        '<p>Read <a href="https://example.com/a">article</a></p><a href="javascript:alert(1)">bad</a><script>secret</script>'
    )
    assert "[article](https://example.com/a)" in text
    assert "javascript:" not in text
    assert "secret" not in text


def test_empty_email_fails() -> None:
    message = EmailMessage()
    message.set_content("")
    with pytest.raises(EmptyEmailError):
        extract_mime(message.as_bytes())


def test_gmail_payload_skips_malformed_part_and_uses_valid_text() -> None:
    payload = {
        "parts": [
            {"mimeType": "text/plain", "body": {"data": "%%%"}},
            {"mimeType": "text/plain", "body": {"data": "dmFsaWQ"}},
        ]
    }

    assert extract_gmail_payload(payload).analysis_text == "valid"
