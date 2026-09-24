"""Additional authored annotations; never alter the frozen 48-case soak corpus."""

from __future__ import annotations

import html
from dataclasses import dataclass

from corpus import Case, _html


@dataclass(frozen=True)
class AnnotatedCase:
    case: Case
    gold: str


def annotated_cases() -> list[AnnotatedCase]:
    cases = []
    for category in ("article", "documentation", "chinese", "short", "table", "code"):
        for variant in range(3):
            title = f"Annotated {category} {variant}"
            paragraphs = [
                f"Evidence {i}: this paragraph describes a reproducible measurement and its limits."
                for i in range(1 if category == "short" else 6)
            ]
            if category == "chinese":
                paragraphs = [
                    f"第{i}段：实验保持相同资源配置，记录所有成功与失败，并完整保留证据和限制。"  # noqa: RUF001
                    for i in range(6)
                ]
            content = f"<h1>{title}</h1>" + "".join(f"<p>{p}</p>" for p in paragraphs)
            gold = [title, *paragraphs]
            critical = list(paragraphs)
            if category == "table":
                content += "<table><tr><th>Model</th><th>Score</th></tr>"
                content += "<tr><td>ReferenceModel</td><td>0.915</td></tr></table>"
                gold.append("Model Score ReferenceModel 0.915")
                critical += ["ReferenceModel", "0.915"]
            if category == "code":
                code = "def compute_evidence(value):\n    return value * 2 + 17"
                content += f"<pre><code>{html.escape(code)}</code></pre>"
                gold.append(code)
                critical += ["compute_evidence(value)", "return value * 2 + 17"]
            link = "https://example.org/evidence"
            content += f'<p>Source: <a href="{link}">Evidence archive</a>.</p>'
            gold.append(f"Source: Evidence archive {link}.")
            critical.append(link)
            if variant == 0:
                content = f"<article>{content}</article>"
            elif variant == 1:
                content = f'<main><div class="content-body">{content}</div></main>'
            else:
                content = f'<div><div id="post-content"><section>{content}</section></div></div>'
            content = (
                "<nav>Unrelated navigation: Home Pricing Login</nav>"
                + content
                + "<footer>Unrelated legal and subscription notices.</footer>"
            )
            case = Case(
                f"annotated-{category}-{variant}",
                category,
                "text/html",
                _html(title, content),
                tuple(critical),
            )
            cases.append(AnnotatedCase(case, "\n".join(gold)))
    return cases
