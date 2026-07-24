from pathlib import Path

import pytest

from two_much_two_read.article_extractor import ArticleExtractionError, extract_article, extract_self_post

FIXTURES = Path(__file__).parent / "fixtures" / "articles"


@pytest.mark.parametrize("name,title", [("article.html", "Readable article title"), ("main.html", "Main layout title")])
def test_extracts_readable_article_layouts(name: str, title: str) -> None:
    extracted = extract_article("text/html", (FIXTURES / name).read_bytes())

    assert extracted.title == title
    assert len(extracted.text) >= 500
    assert "navigation" not in extracted.text.lower()
    assert "share this article" not in extracted.text.lower()


def test_rejects_paywall_teaser_and_sanitizes_self_posts() -> None:
    with pytest.raises(ArticleExtractionError, match="ARTICLE_NO_USABLE_TEXT"):
        extract_article("text/html", (FIXTURES / "paywall.html").read_bytes())

    extracted = extract_self_post("<p>Useful self-post text. </p>" * 40 + "<script>ignore()</script>")

    assert len(extracted.text) >= 500
    assert "ignore" not in extracted.text
