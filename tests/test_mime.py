from email.message import EmailMessage

import pytest

from two_much_two_read.mime import EmptyEmailError, extract_mime, html_to_text


def test_prefers_plain_text() -> None:
    message = EmailMessage()
    message.set_content("plain wins")
    message.add_alternative("<p>html loses</p>", subtype="html")
    assert extract_mime(message.as_bytes()) == "plain wins"


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
