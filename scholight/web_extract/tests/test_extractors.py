from __future__ import annotations

from scholight.models.web_extract import ExtractResponseFormat
from scholight.web_extract.extractors import extract_html, normalize_text, should_render_html

_ARTICLE = """
<html>
  <head>
    <title>Reliable Extraction</title>
    <meta name="author" content="Ada Example">
  </head>
  <body>
    <nav>Home Products Pricing</nav>
    <article>
      <h1>Reliable Extraction</h1>
      <p>This is the central article content with enough detail for the extractor.</p>
      <pre><code class="language-python">print("hello")</code></pre>
    </article>
    <footer>Copyright Example</footer>
  </body>
</html>
"""


def test_extract_html_main_markdown_removes_navigation() -> None:
    result = extract_html(
        _ARTICLE,
        source_url="https://example.com/article",
        output=ExtractResponseFormat.MAIN_MARKDOWN,
    )

    assert "central article content" in result.content
    assert "Home Products Pricing" not in result.content


def test_extract_html_full_markdown_preserves_navigation() -> None:
    result = extract_html(
        _ARTICLE,
        source_url="https://example.com/article",
        output=ExtractResponseFormat.FULL_MARKDOWN,
    )

    assert "Home Products Pricing" in result.content


def test_extract_html_returns_metadata() -> None:
    result = extract_html(
        _ARTICLE,
        source_url="https://example.com/article",
        output=ExtractResponseFormat.MAIN_MARKDOWN,
    )

    assert (result.title, result.author) == ("Reliable Extraction", "Ada Example")


def test_should_render_detects_empty_spa_shell() -> None:
    html = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'

    assert should_render_html(html, extracted_content="") is True


def test_should_render_keeps_complete_static_article() -> None:
    assert should_render_html(_ARTICLE, extracted_content="A" * 500) is False


def test_normalize_json_is_readable() -> None:
    result = normalize_text(b'{"answer":42}', "application/json", charset="utf-8")

    assert result == '{\n  "answer": 42\n}'
