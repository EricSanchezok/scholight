"""Run a bounded open-arrival-rate canary against a Scholight deployment."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import html
import ipaddress
import itertools
import json
import math
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_STANDARD_RATE_CANDIDATES = (1.0, 2.0, 4.0, 8.0, 12.0, 16.0, 20.0)
_THOROUGH_RATE_CANDIDATES = (0.5, 1.0, 2.0, 3.0, 4.0)
_DEFAULT_MAX_STANDARD_RPS = 4.0
_DEFAULT_MAX_THOROUGH_RPS = 1.0
_MAX_STANDARD_RPS = 20.0
_MAX_THOROUGH_RPS = 4.0
_MAX_STAGE_SECONDS = 120
_MAX_IN_FLIGHT = 512
_WARMUP_REQUESTS = 5
_COOLDOWN_SECONDS = 15
_API_KEY_ENV = "SCHOLIGHT_LOAD_TEST_API_KEY"
_SLO_SECONDS = {"standard": 5.0, "thorough": 30.0}
_REQUEST_TIMEOUT_SECONDS = {"standard": 25.0, "thorough": 65.0}
_CRITICAL_CATEGORIES = frozenset({"connect_error", "reset_error", "unexpected_5xx"})
_QUERIES = (
    "retrieval augmented generation evaluation",
    "vision language model reasoning",
    "efficient transformer inference",
    "graph neural network robustness",
    "diffusion models for scientific discovery",
    "multimodal representation learning",
    "large language model alignment",
    "neural information retrieval",
    "reinforcement learning from human feedback",
    "machine learning uncertainty calibration",
    "federated learning privacy",
    "long context language models",
    "causal representation learning",
    "robot learning from demonstrations",
    "synthetic data quality evaluation",
    "sparse mixture of experts",
    "AI agent planning and tool use",
    "document reranking with neural models",
    "foundation models for scientific reasoning",
    "robust evaluation of generative models",
)
SaturationConclusion = Literal[
    "not established",
    "generator limited",
    "overload protected",
    "saturation likely",
]


@dataclass(frozen=True, slots=True)
class RequestResult:
    """One request outcome without retaining response content or credentials."""

    status: int | str
    duration_seconds: float
    degraded: bool
    category: str = "success"
    started_offset_seconds: float = 0.0
    error_sample: str | None = None

    @property
    def successful(self) -> bool:
        return self.category == "success"


@dataclass(slots=True)
class StageSummary:
    """Aggregate offered load and completed outcomes for one stage."""

    strength: str
    target_rps: float
    duration_seconds: int
    offered: int = 0
    scheduled: int = 0
    generator_dropped: int = 0
    results: list[RequestResult] = field(default_factory=list)
    wall_seconds: float = 0.0
    max_consecutive_critical: int = 0

    @property
    def dropped(self) -> int:
        """Compatibility alias for reports made by the earlier canary."""
        return self.generator_dropped

    @property
    def status_counts(self) -> Counter[str]:
        return Counter(str(result.status) for result in self.results)

    @property
    def outcome_counts(self) -> Counter[str]:
        counts = Counter(result.category for result in self.results)
        counts["generator_limited"] += self.generator_dropped
        return counts

    @property
    def completed(self) -> int:
        return len(self.results)

    @property
    def successful(self) -> int:
        return sum(result.successful for result in self.results)

    @property
    def error_count(self) -> int:
        return self.completed - self.successful

    @property
    def error_rate(self) -> float:
        return self.error_count / self.completed if self.completed else 0.0

    @property
    def completed_rps(self) -> float:
        return self.completed / self.wall_seconds if self.wall_seconds else 0.0

    @property
    def goodput_rps(self) -> float:
        return self.successful / self.wall_seconds if self.wall_seconds else 0.0

    def successful_percentile(self, quantile: float) -> float:
        return percentile(
            [result.duration_seconds for result in self.results if result.successful],
            quantile,
        )

    @property
    def p50_seconds(self) -> float:
        return self.successful_percentile(0.50)

    @property
    def p90_seconds(self) -> float:
        return self.successful_percentile(0.90)

    @property
    def p95_seconds(self) -> float:
        return self.successful_percentile(0.95)

    @property
    def p99_seconds(self) -> float:
        return self.successful_percentile(0.99)

    @property
    def max_seconds(self) -> float:
        return max(
            (result.duration_seconds for result in self.results if result.successful),
            default=0.0,
        )


def percentile(values: Sequence[float], quantile: float) -> float:
    """Return a nearest-rank percentile, or zero for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]


def build_rate_plan(maximum: float, *, candidates: Sequence[float]) -> tuple[float, ...]:
    """Build progressive stages that always include the requested bounded maximum."""
    plan = [rate for rate in candidates if rate <= maximum]
    if not plan or not math.isclose(plan[-1], maximum):
        plan.append(maximum)
    return tuple(plan)


def validate_target(base_url: str, *, allow_remote: bool) -> str:
    """Require explicit acknowledgement for a non-loopback target."""
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("--base-url must be an absolute HTTP(S) origin")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("--base-url must not contain a path, query, or fragment")

    is_loopback = parsed.hostname == "localhost"
    with contextlib.suppress(ValueError):
        is_loopback = is_loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    if not is_loopback and not allow_remote:
        raise ValueError("remote targets require --allow-remote")
    return base_url.rstrip("/")


def validate_load_limits(
    *,
    maximum_standard_rps: float,
    maximum_thorough_rps: float | None,
    allow_elevated_load: bool,
) -> None:
    """Bound production load and require explicit acknowledgement above safe defaults."""
    if maximum_standard_rps > _MAX_STANDARD_RPS:
        raise ValueError(f"--max-standard-rps cannot exceed {_MAX_STANDARD_RPS:g}")
    if maximum_thorough_rps is not None and maximum_thorough_rps > _MAX_THOROUGH_RPS:
        raise ValueError(f"--max-thorough-rps cannot exceed {_MAX_THOROUGH_RPS:g}")
    elevated = maximum_standard_rps > _DEFAULT_MAX_STANDARD_RPS or (
        maximum_thorough_rps is not None and maximum_thorough_rps > _DEFAULT_MAX_THOROUGH_RPS
    )
    if elevated and not allow_elevated_load:
        raise ValueError("loads above the default safety limits require --allow-elevated-load")


def evaluate_stage(summary: StageSummary, *, p95_limit_seconds: float | None = None) -> str | None:
    """Return only plan-approved safety stop reasons.

    A single HTTP failure, a generator drop, or an SLO breach is evidence to
    report, not a reason to truncate the experiment.
    """
    del p95_limit_seconds
    if summary.max_consecutive_critical >= 5:
        return "five consecutive connection/reset/unexpected-5xx failures"
    if summary.completed >= 50 and summary.error_rate >= 0.20:
        return "completed error rate reached 20%"
    return None


def build_stage_specs(
    *,
    selected_strength: str,
    maximum_standard_rps: float,
    maximum_thorough_rps: float | None,
    standard_stage_seconds: int = 60,
    thorough_stage_seconds: int = 90,
) -> list[tuple[str, float, int]]:
    """Build independent Standard and Thorough stage families."""
    specs: list[tuple[str, float, int]] = []
    if selected_strength in {"standard", "both"}:
        specs.extend(
            ("standard", rate, standard_stage_seconds)
            for rate in build_rate_plan(
                maximum_standard_rps,
                candidates=_STANDARD_RATE_CANDIDATES,
            )
        )
    if selected_strength in {"thorough", "both"}:
        if maximum_thorough_rps is None:
            if selected_strength == "thorough":
                raise ValueError("--strength thorough requires --max-thorough-rps")
        else:
            specs.extend(
                ("thorough", rate, thorough_stage_seconds)
                for rate in build_rate_plan(
                    maximum_thorough_rps,
                    candidates=_THOROUGH_RATE_CANDIDATES,
                )
            )
    return specs


def classify_saturation(
    stages: Sequence[StageSummary],
    *,
    strength: str,
) -> SaturationConclusion:
    """Classify evidence without equating isolated failures with saturation."""
    selected = [stage for stage in stages if stage.strength == strength]
    if not selected:
        return "not established"

    baseline = next((stage.p95_seconds for stage in selected if stage.successful), 0.0)
    consecutive_plateaus = 0
    for previous, current in itertools.pairwise(selected):
        goodput_plateau = current.goodput_rps <= previous.goodput_rps * 1.05
        service_signal = (
            baseline > 0 and max(current.p95_seconds, current.p99_seconds) > baseline * 2
        ) or current.error_rate >= 0.01
        if goodput_plateau and service_signal:
            consecutive_plateaus += 1
            if consecutive_plateaus >= 2:
                return "saturation likely"
        else:
            consecutive_plateaus = 0

    if any(stage.outcome_counts["capacity_rejected"] for stage in selected):
        return "overload protected"
    if any(stage.generator_dropped for stage in selected):
        return "generator limited"
    return "not established"


def _stage_label(summary: StageSummary) -> str:
    return f"{summary.strength.title()} {summary.target_rps:g} RPS"


def _svg_shell(title: str, description: str, body: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="500" '
        'viewBox="0 0 1200 500" role="img">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<desc>{html.escape(description)}</desc>\n"
        "<style>"
        "text{font-family:Inter,Arial,sans-serif;fill:#25241f}"
        ".title{font-family:Georgia,serif;font-size:26px;font-weight:600}"
        ".axis{font-size:12px;fill:#6f6c63}"
        ".value{font-size:11px;fill:#3f3d37}"
        ".grid{stroke:#dedbd2;stroke-width:1}"
        "</style>\n"
        '<rect width="1200" height="500" fill="#fbfaf6"/>\n'
        f'<text class="title" x="72" y="42">{html.escape(title)}</text>\n'
        f"{body}\n"
        "</svg>\n"
    )


def _empty_chart(title: str, description: str) -> str:
    return _svg_shell(
        title,
        description,
        '<text class="axis" x="600" y="250" text-anchor="middle">No stage data</text>',
    )


def _axis_ticks(maximum: float) -> list[str]:
    body: list[str] = []
    for index in range(5):
        fraction = index / 4
        y = 400 - fraction * 300
        body.append(f'<line class="grid" x1="72" y1="{y}" x2="1160" y2="{y}"/>')
        body.append(
            f'<text class="axis" x="60" y="{y + 4}" text-anchor="end">'
            f"{fraction * maximum:.1f}</text>"
        )
    return body


def _x_label(body: list[str], stage: StageSummary, center: float) -> None:
    body.append(
        f'<text class="axis" x="{center:.1f}" y="432" text-anchor="middle">'
        f"{html.escape(_stage_label(stage))}</text>"
    )


def _throughput_chart(stages: Sequence[StageSummary]) -> str:
    title = "Offered load, completed throughput, and successful goodput"
    description = "Requests per second by load stage; generator drops are not server failures."
    if not stages:
        return _empty_chart(title, description)
    maximum = max(1.0, max(stage.target_rps for stage in stages) * 1.15)
    width = 1088 / len(stages)
    series = (
        ("offered", "#3154c9", lambda stage: stage.target_rps),
        ("completed", "#8b887f", lambda stage: stage.completed_rps),
        ("goodput", "#2f6b4f", lambda stage: stage.goodput_rps),
    )
    body = _axis_ticks(maximum)
    for index, stage in enumerate(stages):
        center = 72 + (index + 0.5) * width
        points = []
        for series_index, (_, color, value_for) in enumerate(series):
            value = value_for(stage)
            x = center + (series_index - 1) * 18
            y = 400 - value / maximum * 300
            body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}"/>')
            points.append((x, y))
        _x_label(body, stage, center)
        del points
    for series_index, (label, color, _) in enumerate(series):
        x = 800 + series_index * 120
        body.append(f'<circle cx="{x}" cy="38" r="5" fill="{color}"/>')
        body.append(f'<text class="axis" x="{x + 10}" y="42">{label}</text>')
    body.append('<text class="axis" x="18" y="250" transform="rotate(-90 18 250)">RPS</text>')
    return _svg_shell(title, description, "\n".join(body))


def _latency_chart(stages: Sequence[StageSummary]) -> str:
    title = "Successful request latency percentiles"
    description = "P50, P90, P95, P99, and maximum latency with initial SLO lines."
    if not stages:
        return _empty_chart(title, description)
    maximum = max(
        1.0,
        max(max(stage.max_seconds, _SLO_SECONDS[stage.strength]) for stage in stages) * 1.15,
    )
    width = 1088 / len(stages)
    series = (
        ("p50", "#3154c9", lambda stage: stage.p50_seconds),
        ("p90", "#6f83cf", lambda stage: stage.p90_seconds),
        ("p95", "#8b887f", lambda stage: stage.p95_seconds),
        ("p99", "#c09352", lambda stage: stage.p99_seconds),
        ("max", "#ba4434", lambda stage: stage.max_seconds),
    )
    body = _axis_ticks(maximum)
    for index, stage in enumerate(stages):
        center = 72 + (index + 0.5) * width
        points: list[str] = []
        for series_index, (_, color, value_for) in enumerate(series):
            value = value_for(stage)
            x = center + (series_index - 2) * 10
            y = 400 - value / maximum * 300
            body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}"/>')
            points.append(f"{x:.1f},{y:.1f}")
        body.append(
            f'<polyline points="{" ".join(points)}" fill="none" stroke="#c8c5bc" stroke-width="1"/>'
        )
        _x_label(body, stage, center)
    for strength, color in (("standard", "#3154c9"), ("thorough", "#ba4434")):
        y = 400 - _SLO_SECONDS[strength] / maximum * 300
        body.append(
            f'<line x1="72" y1="{y:.1f}" x2="1160" y2="{y:.1f}" '
            f'stroke="{color}" stroke-dasharray="5 5" opacity=".55"/>'
        )
    for series_index, (label, color, _) in enumerate(series):
        x = 690 + series_index * 92
        body.append(f'<circle cx="{x}" cy="38" r="4" fill="{color}"/>')
        body.append(f'<text class="axis" x="{x + 9}" y="42">{label}</text>')
    body.append('<text class="axis" x="18" y="250" transform="rotate(-90 18 250)">seconds</text>')
    return _svg_shell(title, description, "\n".join(body))


def _errors_chart(stages: Sequence[StageSummary]) -> str:
    title = "HTTP, network, capacity, and generator outcomes"
    description = "Exact non-success outcomes grouped separately from generator limitation."
    if not stages:
        return _empty_chart(title, description)
    categories = (
        "rate_limited",
        "capacity_rejected",
        "unexpected_5xx",
        "connect_error",
        "reset_error",
        "read_timeout",
        "write_timeout",
        "pool_timeout",
        "other_error",
        "generator_limited",
    )
    colors = (
        "#b8a46a",
        "#3154c9",
        "#ba4434",
        "#5e5b55",
        "#7d5d5d",
        "#99714d",
        "#a58470",
        "#75669a",
        "#a7a39a",
        "#d3d0c7",
    )
    maximum = max(
        1, max(sum(stage.outcome_counts[name] for name in categories) for stage in stages)
    )
    width = 1088 / len(stages)
    body = _axis_ticks(float(maximum))
    for index, stage in enumerate(stages):
        center = 72 + (index + 0.5) * width
        current_y = 400.0
        for category, color in zip(categories, colors, strict=True):
            count = stage.outcome_counts[category]
            if not count:
                continue
            height = count / maximum * 300
            current_y -= height
            body.append(
                f'<rect x="{center - min(60, width * 0.3):.1f}" y="{current_y:.1f}" '
                f'width="{min(120, width * 0.6):.1f}" height="{height:.1f}" fill="{color}"/>'
            )
        _x_label(body, stage, center)
    body.append('<text class="axis" x="18" y="250" transform="rotate(-90 18 250)">count</text>')
    return _svg_shell(title, description, "\n".join(body))


def _stage_payload(stage: StageSummary) -> dict[str, object]:
    samples = [result.error_sample for result in stage.results if result.error_sample][:5]
    return {
        "strength": stage.strength,
        "target_rps": stage.target_rps,
        "duration_seconds": stage.duration_seconds,
        "wall_seconds": round(stage.wall_seconds, 6),
        "offered": stage.offered,
        "scheduled": stage.scheduled,
        "completed": stage.completed,
        "successful": stage.successful,
        "goodput_rps": round(stage.goodput_rps, 6),
        "completed_rps": round(stage.completed_rps, 6),
        "generator_dropped": stage.generator_dropped,
        "error_rate": round(stage.error_rate, 6),
        "p50_seconds": round(stage.p50_seconds, 6),
        "p90_seconds": round(stage.p90_seconds, 6),
        "p95_seconds": round(stage.p95_seconds, 6),
        "p99_seconds": round(stage.p99_seconds, 6),
        "max_seconds": round(stage.max_seconds, 6),
        "status_counts": dict(stage.status_counts),
        "outcome_counts": dict(stage.outcome_counts),
        "error_samples": samples,
    }


def _report_html(
    *,
    base_url: str,
    started_at: datetime,
    finished_at: datetime,
    stages: Sequence[StageSummary],
) -> str:
    conclusions = {
        strength: classify_saturation(stages, strength=strength)
        for strength in ("standard", "thorough")
        if any(stage.strength == strength for stage in stages)
    }
    rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(_stage_label(stage))}</td>"
        f"<td>{stage.offered}</td><td>{stage.scheduled}</td><td>{stage.completed}</td>"
        f"<td>{stage.successful}</td><td>{stage.goodput_rps:.2f}</td>"
        f"<td>{stage.p50_seconds:.2f}s</td><td>{stage.p90_seconds:.2f}s</td>"
        f"<td>{stage.p95_seconds:.2f}s</td><td>{stage.p99_seconds:.2f}s</td>"
        f"<td>{stage.max_seconds:.2f}s</td>"
        f"<td>{html.escape(str(dict(stage.outcome_counts)))}</td>"
        "</tr>"
        for stage in stages
    )
    conclusion_html = "".join(
        f"<p><strong>{html.escape(strength.title())}:</strong> {html.escape(conclusion)}</p>"
        for strength, conclusion in conclusions.items()
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scholight bounded canary report</title>
<style>
body{{margin:0;background:#f4f2ec;color:#25241f;font-family:Inter,Arial,sans-serif}}
main{{max-width:1240px;margin:0 auto;padding:48px 28px 72px}}
h1,h2{{font-family:Georgia,serif}} h1{{font-size:42px;margin:0 0 8px}}
.meta{{color:#6f6c63;margin-bottom:40px}} section{{margin:36px 0}}
.conclusion{{border-block:1px solid #dedbd2;padding:18px 0}}
img{{display:block;width:100%;height:auto;border:1px solid #dedbd2;background:#fbfaf6}}
table{{width:100%;border-collapse:collapse;background:#fbfaf6;font-size:13px}}
th,td{{padding:10px;text-align:left;border-bottom:1px solid #dedbd2}}
th{{color:#6f6c63;font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
code{{font-family:ui-monospace,monospace}}
</style>
</head>
<body><main>
<h1>Scholight bounded production canary</h1>
<p class="meta"><code>{html.escape(base_url)}</code><br>
{html.escape(started_at.isoformat())} to {html.escape(finished_at.isoformat())}</p>
<section class="conclusion"><h2>Saturation evidence</h2>{conclusion_html}</section>
<section><img src="throughput-and-goodput.svg" alt="Offered, completed, and goodput chart"></section>
<section><img src="latency-percentiles.svg" alt="Latency percentile chart"></section>
<section><img src="outcome-breakdown.svg" alt="Outcome breakdown chart"></section>
<section><h2>Exact stage counts</h2>
<table><thead><tr><th>Stage</th><th>Offered</th><th>Scheduled</th><th>Completed</th>
<th>Successful</th><th>Goodput</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th>
<th>Max</th><th>Outcomes</th></tr></thead><tbody>{rows}</tbody></table></section>
</main></body></html>
"""


def write_report(
    output_dir: Path,
    *,
    base_url: str,
    started_at: datetime,
    finished_at: datetime,
    stages: Sequence[StageSummary],
) -> tuple[Path, ...]:
    """Write machine-readable results and dependency-free professional charts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "base_url": base_url,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "conclusions": {
            strength: classify_saturation(stages, strength=strength)
            for strength in ("standard", "thorough")
            if any(stage.strength == strength for stage in stages)
        },
        "stages": [_stage_payload(stage) for stage in stages],
    }
    results_path = output_dir / "results.json"
    results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    csv_path = output_dir / "requests.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "stage",
                "strength",
                "target_rps",
                "status",
                "category",
                "duration_seconds",
                "degraded",
                "started_offset_seconds",
            ),
        )
        writer.writeheader()
        for stage_index, stage in enumerate(stages, start=1):
            for result in stage.results:
                writer.writerow(
                    {
                        "stage": stage_index,
                        "strength": stage.strength,
                        "target_rps": stage.target_rps,
                        "status": result.status,
                        "category": result.category,
                        "duration_seconds": f"{result.duration_seconds:.6f}",
                        "degraded": result.degraded,
                        "started_offset_seconds": f"{result.started_offset_seconds:.6f}",
                    }
                )

    throughput_path = output_dir / "throughput-and-goodput.svg"
    throughput_path.write_text(_throughput_chart(stages), encoding="utf-8")
    latency_path = output_dir / "latency-percentiles.svg"
    latency_path.write_text(_latency_chart(stages), encoding="utf-8")
    errors_path = output_dir / "outcome-breakdown.svg"
    errors_path.write_text(_errors_chart(stages), encoding="utf-8")
    report_path = output_dir / "report.html"
    report_path.write_text(
        _report_html(
            base_url=base_url,
            started_at=started_at,
            finished_at=finished_at,
            stages=stages,
        ),
        encoding="utf-8",
    )
    return report_path, results_path, csv_path, throughput_path, latency_path, errors_path


def _response_category(response: httpx.Response) -> tuple[str, str | None]:
    error_code: str | None = None
    if response.headers.get("content-type", "").startswith("application/json"):
        with contextlib.suppress(ValueError):
            payload = response.json()
            if isinstance(payload, dict):
                candidate = payload.get("code")
                if not isinstance(candidate, str):
                    detail = payload.get("detail")
                    if isinstance(detail, dict):
                        candidate = detail.get("code")
                if isinstance(candidate, str):
                    error_code = candidate[:80]
    if 200 <= response.status_code < 400:
        return "success", None
    if response.status_code == 429:
        return "rate_limited", error_code or "http_429"
    if response.status_code == 503 and error_code == "search_capacity_exceeded":
        return "capacity_rejected", error_code
    if response.status_code >= 500:
        return "unexpected_5xx", error_code or f"http_{response.status_code}"
    return "other_error", error_code or f"http_{response.status_code}"


def _exception_category(exc: httpx.HTTPError) -> str:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "connect_error"
    if isinstance(exc, (httpx.ReadError, httpx.RemoteProtocolError)):
        return "reset_error"
    if isinstance(exc, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, httpx.WriteTimeout):
        return "write_timeout"
    if isinstance(exc, httpx.PoolTimeout):
        return "pool_timeout"
    return "other_error"


async def _request(
    client: httpx.AsyncClient,
    *,
    url: str,
    api_key: str,
    query: str,
    strength: str,
    started_offset_seconds: float,
    abort_event: asyncio.Event | None = None,
) -> RequestResult:
    del abort_event
    started = time.perf_counter()
    try:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": query, "strength": strength, "limit": 5},
        )
        category, sample = _response_category(response)
        degraded = False
        if category == "success" and response.headers.get("content-type", "").startswith(
            "application/json"
        ):
            with contextlib.suppress(ValueError):
                payload = response.json()
                degraded = isinstance(payload, dict) and payload.get("degraded") is True
        return RequestResult(
            status=response.status_code,
            duration_seconds=time.perf_counter() - started,
            degraded=degraded,
            category=category,
            started_offset_seconds=started_offset_seconds,
            error_sample=sample,
        )
    except httpx.HTTPError as exc:
        category = _exception_category(exc)
        return RequestResult(
            status=type(exc).__name__,
            duration_seconds=time.perf_counter() - started,
            degraded=False,
            category=category,
            started_offset_seconds=started_offset_seconds,
            error_sample=type(exc).__name__,
        )


async def run_stage(
    client: httpx.AsyncClient,
    *,
    url: str,
    api_key: str,
    strength: str,
    target_rps: float,
    duration_seconds: int,
    run_started: float,
) -> StageSummary:
    """Run one open-arrival stage with a dynamic, bounded generator pool."""
    summary = StageSummary(
        strength=strength,
        target_rps=target_rps,
        duration_seconds=duration_seconds,
    )
    request_timeout = _REQUEST_TIMEOUT_SECONDS[strength]
    max_in_flight = min(
        _MAX_IN_FLIGHT,
        max(1, math.ceil(target_rps * request_timeout * 1.25)),
    )
    pending: set[asyncio.Task[RequestResult]] = set()
    request_count = max(1, round(target_rps * duration_seconds))
    summary.offered = request_count
    started = time.perf_counter()
    consecutive_critical = 0

    async def collect(task: asyncio.Task[RequestResult]) -> None:
        nonlocal consecutive_critical
        result = await task
        summary.results.append(result)
        if result.category in _CRITICAL_CATEGORIES:
            consecutive_critical += 1
            summary.max_consecutive_critical = max(
                summary.max_consecutive_critical,
                consecutive_critical,
            )
        else:
            consecutive_critical = 0

    collectors: set[asyncio.Task[None]] = set()
    for index in range(request_count):
        due_at = started + index / target_rps
        await asyncio.sleep(max(0.0, due_at - time.perf_counter()))
        pending = {task for task in pending if not task.done()}
        collectors = {task for task in collectors if not task.done()}
        if len(pending) >= max_in_flight:
            summary.generator_dropped += 1
            continue
        request_task = asyncio.create_task(
            _request(
                client,
                url=url,
                api_key=api_key,
                query=_QUERIES[index % len(_QUERIES)],
                strength=strength,
                started_offset_seconds=time.perf_counter() - run_started,
            )
        )
        pending.add(request_task)
        collectors.add(asyncio.create_task(collect(request_task)))
        summary.scheduled += 1

    if collectors:
        await asyncio.gather(*collectors)
    summary.wall_seconds = time.perf_counter() - started
    return summary


async def _warm_up(
    client: httpx.AsyncClient,
    *,
    url: str,
    api_key: str,
    strength: str,
    run_started: float,
) -> None:
    for index in range(_WARMUP_REQUESTS):
        result = await _request(
            client,
            url=url,
            api_key=api_key,
            query=_QUERIES[index],
            strength=strength,
            started_offset_seconds=time.perf_counter() - run_started,
        )
        if not result.successful:
            print(f"warm-up {strength}: {result.category} ({result.status})")


def _print_summary(summary: StageSummary) -> None:
    print(
        f"{summary.strength} target={summary.target_rps:g}rps "
        f"offered={summary.offered} scheduled={summary.scheduled} "
        f"completed={summary.completed} successful={summary.successful} "
        f"goodput={summary.goodput_rps:.2f}rps "
        f"p50/p90/p95/p99={summary.p50_seconds:.2f}/{summary.p90_seconds:.2f}/"
        f"{summary.p95_seconds:.2f}/{summary.p99_seconds:.2f}s "
        f"outcomes={dict(summary.outcome_counts)}"
    )


async def run_canary(
    *,
    base_url: str,
    api_key: str,
    selected_strength: str,
    stage_seconds: int | None,
    maximum_standard_rps: float,
    maximum_thorough_rps: float | None,
    cooldown_seconds: int = _COOLDOWN_SECONDS,
) -> tuple[int, datetime, datetime, list[StageSummary]]:
    """Run all requested stages, stopping only on approved safety evidence."""
    started_at = datetime.now(UTC)
    run_started = time.perf_counter()
    summaries: list[StageSummary] = []
    max_connections = min(
        _MAX_IN_FLIGHT,
        max(
            24,
            math.ceil(
                max(
                    maximum_standard_rps * _REQUEST_TIMEOUT_SECONDS["standard"],
                    (maximum_thorough_rps or 0) * _REQUEST_TIMEOUT_SECONDS["thorough"],
                )
                * 1.25
            ),
        ),
    )
    timeout = httpx.Timeout(65.0, connect=5.0, read=65.0, write=10.0, pool=1.0)
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=min(max_connections, 64),
    )
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        specs = build_stage_specs(
            selected_strength=selected_strength,
            maximum_standard_rps=maximum_standard_rps,
            maximum_thorough_rps=maximum_thorough_rps,
            standard_stage_seconds=stage_seconds or 60,
            thorough_stage_seconds=stage_seconds or 90,
        )
        previous_strength: str | None = None
        for index, (strength, rate, duration) in enumerate(specs):
            if strength != previous_strength:
                await _warm_up(
                    client,
                    url=f"{base_url}/api/search",
                    api_key=api_key,
                    strength=strength,
                    run_started=run_started,
                )
                previous_strength = strength
            summary = await run_stage(
                client,
                url=f"{base_url}/api/search",
                api_key=api_key,
                strength=strength,
                target_rps=rate,
                duration_seconds=duration,
                run_started=run_started,
            )
            summaries.append(summary)
            _print_summary(summary)
            if reason := evaluate_stage(summary):
                print(f"STOP: {reason}")
                return 1, started_at, datetime.now(UTC), summaries
            if index < len(specs) - 1 and cooldown_seconds:
                await asyncio.sleep(cooldown_seconds)
    return 0, started_at, datetime.now(UTC), summaries


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Scholight origin without /api")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="acknowledge that the target is not loopback",
    )
    parser.add_argument(
        "--stage-seconds",
        type=int,
        choices=range(5, _MAX_STAGE_SECONDS + 1),
        metavar="5..120",
        help="test-only override; production defaults are Standard 60s and Thorough 90s",
    )
    parser.add_argument(
        "--max-standard-rps",
        type=_positive_float,
        default=4.0,
        help=f"maximum Standard rate; hard limit {_MAX_STANDARD_RPS:g}",
    )
    parser.add_argument(
        "--max-thorough-rps",
        type=_positive_float,
        help=f"include Thorough stages up to this rate; hard limit {_MAX_THOROUGH_RPS:g}",
    )
    parser.add_argument(
        "--allow-elevated-load",
        action="store_true",
        help="acknowledge rates above the conservative defaults",
    )
    parser.add_argument(
        "--strength",
        choices=("standard", "thorough", "both"),
        default="both",
    )
    parser.add_argument(
        "--report-root",
        type=Path,
        default=Path("data/load-tests"),
    )
    return parser.parse_args()


def main(env: Mapping[str, str] = os.environ) -> int:
    """CLI entry point."""
    args = _parse_args()
    try:
        base_url = validate_target(args.base_url, allow_remote=args.allow_remote)
        validate_load_limits(
            maximum_standard_rps=args.max_standard_rps,
            maximum_thorough_rps=args.max_thorough_rps,
            allow_elevated_load=args.allow_elevated_load,
        )
    except ValueError as exc:
        print(f"configuration error: {exc}")
        return 2
    api_key = env.get(_API_KEY_ENV, "")
    if not api_key.startswith("sk_live_"):
        print(f"configuration error: {_API_KEY_ENV} must contain an Access Key")
        return 2
    try:
        exit_code, started_at, finished_at, stages = asyncio.run(
            run_canary(
                base_url=base_url,
                api_key=api_key,
                selected_strength=args.strength,
                stage_seconds=args.stage_seconds,
                maximum_standard_rps=args.max_standard_rps,
                maximum_thorough_rps=args.max_thorough_rps,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("interrupted by user")
        return 130
    except ValueError as exc:
        print(f"configuration error: {exc}")
        return 2
    report_dir = args.report_root / started_at.strftime("%Y%m%dT%H%M%SZ")
    write_report(
        report_dir,
        base_url=base_url,
        started_at=started_at,
        finished_at=finished_at,
        stages=stages,
    )
    print(f"report={report_dir / 'report.html'}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
