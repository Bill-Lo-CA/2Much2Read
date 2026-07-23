from email.message import EmailMessage

import pytest

from two_much_two_read.mime import EmptyEmailError, extract_gmail_payload, extract_mime, html_to_text


@pytest.mark.parametrize(("plain", "html"), [("plain wins", None), ("plain wins", "<p>html loses</p>")])
def test_extract_mime_returns_plain_text_for_supported_structures(plain: str, html: str | None) -> None:
    message = EmailMessage()
    message.set_content(plain)
    if html is not None:
        message.add_alternative(html, subtype="html")

    assert extract_mime(message.as_bytes()) == plain


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

    assert extract_gmail_payload(payload) == "valid"
