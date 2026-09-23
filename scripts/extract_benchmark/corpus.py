"""Versioned, self-owned fixtures; no remote content or production credentials."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    name: str
    category: str
    mime: str
    body: bytes
    expected: tuple[str, ...]
    status: int = 200


def _html(title: str, body: str) -> bytes:
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{title}</title></head><body>{body}</body></html>"
    ).encode()


def _pdf(text: str) -> bytes:
    stream = f"BT /F1 14 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{i} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(result)


def corpus() -> list[Case]:
    cases: list[Case] = []
    for category, count in [("article", 6), ("documentation", 6), ("chinese", 6)]:
        for i in range(count):
            key = f"{category}-{i}"
            sentence = (
                f"测试段落 {key}: 提取必须保留关键结论、证据与来源链接。"
                if category == "chinese"
                else f"Document {key} preserves the evidence and the central research conclusion."
            )
            paragraphs = "".join(
                f"<p>{sentence} Section {j}: "
                + "Reliable extraction keeps the complete paragraph and its context. " * 4
                + "</p>"
                for j in range(600 if i == 5 else 6 + i)
            )
            body = f"<article><h1>{key}</h1>{paragraphs}</article>"
            cases.append(Case(key, category, "text/html", _html(key, body), (sentence,)))
    for category in ["short", "table", "code"]:
        for i in range(4):
            key = f"{category}-{i}"
            token = f"Evidence-{key}"
            paragraph = f"<p>{token}: " + "A documented result retains its context. " * 12 + "</p>"
            expected = [token]
            if category == "table":
                paragraph += (
                    "<table><tr><th>Model</th><th>Score</th></tr>"
                    f"<tr><td>FixtureModel{i}</td><td>0.91</td></tr></table>"
                )
                expected += [f"FixtureModel{i}", "0.91"]
            elif category == "code":
                code = f"def fixture_{i}(value):\n    return value + {i}"
                paragraph += f"<pre><code>{html.escape(code)}</code></pre>"
                expected += [f"fixture_{i}", f"return value + {i}"]
            else:
                paragraph = f"<p>{token}: a concise note with a clear conclusion.</p>"
            cases.append(Case(key, category, "text/html", _html(key, paragraph), tuple(expected)))
    for i in range(6):
        key = f"javascript-{i}"
        token = f"Hydrated-evidence-{i}"
        content = f"<article><h1>{token}</h1><p>" + "Rendered evidence is available. " * 18
        content += "</p></article>"
        script = (
            "setTimeout(() => {document.getElementById('root').innerHTML = "
            + json.dumps(content)
            + ";}, 40);"
        )
        body = f"<div id='root'>Enable JavaScript</div><script>{script}</script>"
        cases.append(Case(key, "javascript", "text/html", _html(key, body), (token,)))
    for i in range(6):
        key = f"structured-{i}"
        token = f"Structured-evidence-{i}"
        mime = "application/json" if i % 2 == 0 else "application/xml"
        body = json.dumps({"evidence": token}) if i % 2 == 0 else f"<evidence>{token}</evidence>"
        cases.append(Case(key, "structured", mime, body.encode(), (token,)))
    for i in range(6):
        token = f"PDF evidence number {i}"
        cases.append(
            Case(
                f"pdf-{i}",
                "pdf",
                "application/pdf",
                _pdf(token) if i < 5 else b"%PDF-broken",
                (token,) if i < 5 else (),
                status=200 if i < 5 else 422,
            )
        )
    return cases
