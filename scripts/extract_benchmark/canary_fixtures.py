"""Generate small, self-owned public canaries; no external content or secrets."""
# ruff: noqa: RUF001

import subprocess  # nosec B404
from pathlib import Path

from corpus import _html, _pdf

ROOT = Path(__file__).resolve().parents[2] / "frontend/public/extract-canary"
ARTICLE = """<article><h1>Scholight extraction canary</h1>
<p>ScholightStaticEvidence2026. This document is a deterministic operational fixture.
It contains no account information, research data or external dependencies. Its
paragraphs check that extraction preserves complete sentences across pagination.</p>
<p>中文固定证据：正文、表格、代码和链接应在提取后保留。此页面仅用于低频验收，不包含个人信息。</p>
<h2 id="evidence">Evidence</h2>
<table><tr><th>Sample</th><th>Value</th></tr><tr><td>CanaryTable</td><td>42</td></tr></table>
<pre><code>def canary(value):
    return value + 42</code></pre>
<p>The final paragraph marks the end of the immutable document with
ScholightStaticEnd2026. See the <a href="#evidence">fixture evidence</a>.</p></article>"""


def write() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "static.html").write_bytes(_html("Scholight static canary", ARTICLE) + b"\n")
    (ROOT / "document.pdf").write_bytes(_pdf("Scholight PDF canary evidence 2026."))
    (ROOT / "javascript.html").write_bytes(
        _html(
            "Scholight JavaScript canary",
            """<main id="root">Enable JavaScript</main><script>
setTimeout(() => {
  document.getElementById('root').innerHTML = '<article><h1>ScholightJSEvidence2026</h1>'
    + '<p>This fixed paragraph is inserted by JavaScript after the page loads. '
    + 'Successful extraction confirms that the browser rendered the document, '
    + 'waited for its content and preserved this evidence in the returned result. '
    + 'The sample makes no network requests and changes no account state.</p></article>';
}, 40);
</script>""",
        )
        + b"\n"
    )
    # Use the repository's pinned formatter so generated HTML passes frontend CI.
    subprocess.run(  # nosec
        [
            str(ROOT.parents[1] / "node_modules/.bin/prettier"),
            "--write",
            str(ROOT / "static.html"),
            str(ROOT / "javascript.html"),
        ],
        check=True,
    )


if __name__ == "__main__":
    write()
