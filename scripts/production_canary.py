"""Run a bounded, progressively increasing search canary against Scholight."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import html
import ipaddress
import json
import math
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_STANDARD_RATE_CANDIDATES = (0.5, 1.0, 2.0, 4.0)
_THOROUGH_RATE_CANDIDATES = (0.1, 0.2, 0.4)
_MAX_STANDARD_RPS = 10.0
_MAX_THOROUGH_RPS = 1.0
_MAX_STAGE_SECONDS = 120
_MAX_IN_FLIGHT = 32
_API_KEY_ENV = "SCHOLIGHT_LOAD_TEST_API_KEY"
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


@dataclass(frozen=True, slots=True)
class RequestResult:
    """One bounded request outcome without retaining response content."""

    status: int | str
    duration_seconds: float
    degraded: bool
    started_offset_seconds: float = 0.0


@dataclass(slots=True)
class StageSummary:
    """Aggregate inputs and results for one constant-arrival-rate stage."""

    strength: str
    target_rps: float
    duration_seconds: int
    results: list[RequestResult] = field(default_factory=list)
    dropped: int = 0
    wall_seconds: float = 0.0

    @property
    def status_counts(self) -> Counter[str]:
        return Counter(str(result.status) for result in self.results)

    @property
    def error_count(self) -> int:
        return sum(
            not isinstance(result.status, int) or result.status < 200 or result.status >= 400
            for result in self.results
        )

    @property
    def p50_seconds(self) -> float:
        return percentile(
            [result.duration_seconds for result in self.results],
            0.50,
        )

    @property
    def p95_seconds(self) -> float:
        return percentile(
            [result.duration_seconds for result in self.results],
            0.95,
        )

    @property
    def max_seconds(self) -> float:
        return max((result.duration_seconds for result in self.results), default=0.0)


def percentile(values: Sequence[float], quantile: float) -> float:
    """Return the nearest-rank percentile for a non-empty or empty sequence."""
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
    """Validate the target and require explicit confirmation for non-loopback hosts."""
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        msg = "--base-url must be an absolute HTTP(S) origin"
        raise ValueError(msg)
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        msg = "--base-url must not contain a path, query, or fragment"
        raise ValueError(msg)

    hostname = parsed.hostname
    is_loopback = hostname == "localhost"
    with contextlib.suppress(ValueError):
        is_loopback = is_loopback or ipaddress.ip_address(hostname).is_loopback
    if not is_loopback and not allow_remote:
        msg = "remote targets require --allow-remote"
        raise ValueError(msg)
    return base_url.rstrip("/")


def evaluate_stage(summary: StageSummary, *, p95_limit_seconds: float) -> str | None:
    """Return a stop reason when a stage breaches a conservative safety boundary."""
    if any(isinstance(result.status, int) and result.status >= 500 for result in summary.results):
        return "server error observed"
    if summary.dropped:
        return "local in-flight limit reached"
    total = len(summary.results)
    if total and summary.error_count / total > 0.01:
        return "error rate exceeded 1%"
    if summary.p95_seconds > p95_limit_seconds:
        return f"p95 latency exceeded {p95_limit_seconds:.2f}s"
    return None


def _stage_label(summary: StageSummary) -> str:
    strength = "Standard" if summary.strength == "standard" else "Thorough"
    return f"{strength} {summary.target_rps:g} RPS"


def _svg_shell(title: str, description: str, body: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="480" '
        'viewBox="0 0 1200 480" role="img">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<desc>{html.escape(description)}</desc>\n"
        "<style>"
        "text{font-family:Inter,Arial,sans-serif;fill:#25241f}"
        ".title{font-family:Georgia,serif;font-size:26px;font-weight:600}"
        ".axis{font-size:13px;fill:#6f6c63}"
        ".value{font-size:12px;fill:#3f3d37}"
        ".grid{stroke:#dedbd2;stroke-width:1}"
        "</style>\n"
        '<rect width="1200" height="480" fill="#fbfaf6"/>\n'
        f'<text class="title" x="72" y="42">{html.escape(title)}</text>\n'
        f"{body}\n"
        "</svg>\n"
    )


def _empty_chart(title: str, description: str) -> str:
    body = '<text class="axis" x="600" y="245" text-anchor="middle">No stage data</text>'
    return _svg_shell(title, description, body)


def _axis_ticks(maximum: float, *, chart_height: float, top: float, left: float) -> list[str]:
    ticks: list[str] = []
    for index in range(5):
        fraction = index / 4
        y = top + chart_height - fraction * chart_height
        value = fraction * maximum
        ticks.append(f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="1160" y2="{y:.1f}"/>')
        ticks.append(
            f'<text class="axis" x="{left - 12}" y="{y + 4:.1f}" '
            f'text-anchor="end">{value:.1f}</text>'
        )
    return ticks


def _latency_chart(stages: Sequence[StageSummary]) -> str:
    title = "Latency by stage"
    description = "P50, P95, and maximum response time in seconds for each load stage."
    if not stages:
        return _empty_chart(title, description)

    left, top, chart_width, chart_height = 72.0, 80.0, 1088.0, 310.0
    maximum = max(1.0, max(stage.max_seconds for stage in stages) * 1.15)
    group_width = chart_width / len(stages)
    bar_width = min(34.0, group_width / 5)
    series = (
        ("p50", "#3154c9", lambda stage: stage.p50_seconds),
        ("p95", "#8b887f", lambda stage: stage.p95_seconds),
        ("max", "#c46b3d", lambda stage: stage.max_seconds),
    )
    body = _axis_ticks(maximum, chart_height=chart_height, top=top, left=left)
    for stage_index, stage in enumerate(stages):
        center = left + (stage_index + 0.5) * group_width
        for series_index, (_, color, value_for) in enumerate(series):
            value = value_for(stage)
            height = value / maximum * chart_height
            x = center + (series_index - 1) * (bar_width + 5) - bar_width / 2
            y = top + chart_height - height
            body.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
                f'height="{height:.1f}" fill="{color}"/>'
            )
            body.append(
                f'<text class="value" x="{x + bar_width / 2:.1f}" y="{max(68, y - 6):.1f}" '
                f'text-anchor="middle">{value:.2f}s</text>'
            )
        body.append(
            f'<text class="axis" x="{center:.1f}" y="420" text-anchor="middle">'
            f"{html.escape(_stage_label(stage))}</text>"
        )
    for index, (label, color, _) in enumerate(series):
        x = 865 + index * 95
        body.append(f'<rect x="{x}" y="30" width="12" height="12" fill="{color}"/>')
        body.append(f'<text class="axis" x="{x + 18}" y="41">{label}</text>')
    body.append('<text class="axis" x="18" y="235" transform="rotate(-90 18 235)">seconds</text>')
    return _svg_shell(title, description, "\n".join(body))


def _outcomes_chart(stages: Sequence[StageSummary]) -> str:
    title = "Request outcomes"
    description = "Successful, client-error, server-error, and network-error counts by stage."
    if not stages:
        return _empty_chart(title, description)

    left, top, chart_width, chart_height = 72.0, 80.0, 1088.0, 310.0
    maximum = max(1, max(len(stage.results) + stage.dropped for stage in stages))
    group_width = chart_width / len(stages)
    bar_width = min(72.0, group_width * 0.55)
    colors = {
        "success": "#3154c9",
        "client": "#b8a46a",
        "server": "#ba4434",
        "network": "#5e5b55",
        "dropped": "#d3d0c7",
    }
    body = _axis_ticks(float(maximum), chart_height=chart_height, top=top, left=left)
    for stage_index, stage in enumerate(stages):
        counts = Counter(
            "success"
            if isinstance(result.status, int) and 200 <= result.status < 400
            else "client"
            if isinstance(result.status, int) and 400 <= result.status < 500
            else "server"
            if isinstance(result.status, int)
            else "network"
            for result in stage.results
        )
        counts["dropped"] = stage.dropped
        center = left + (stage_index + 0.5) * group_width
        current_y = top + chart_height
        for category in ("success", "client", "server", "network", "dropped"):
            count = counts[category]
            if not count:
                continue
            height = count / maximum * chart_height
            current_y -= height
            body.append(
                f'<rect x="{center - bar_width / 2:.1f}" y="{current_y:.1f}" '
                f'width="{bar_width:.1f}" height="{height:.1f}" fill="{colors[category]}"/>'
            )
        body.append(
            f'<text class="value" x="{center:.1f}" y="{current_y - 7:.1f}" '
            f'text-anchor="middle">{len(stage.results)}/{len(stage.results) + stage.dropped}</text>'
        )
        body.append(
            f'<text class="axis" x="{center:.1f}" y="420" text-anchor="middle">'
            f"{html.escape(_stage_label(stage))}</text>"
        )
    body.append('<text class="axis" x="18" y="235" transform="rotate(-90 18 235)">requests</text>')
    return _svg_shell(title, description, "\n".join(body))


def _timeline_chart(stages: Sequence[StageSummary]) -> str:
    title = "Request latency timeline"
    description = "Individual response times plotted by request start time."
    requests = [(stage.strength, result) for stage in stages for result in stage.results]
    if not requests:
        return _empty_chart(title, description)

    left, top, chart_width, chart_height = 72.0, 80.0, 1088.0, 310.0
    maximum_x = max(1.0, max(result.started_offset_seconds for _, result in requests))
    maximum_y = max(1.0, max(result.duration_seconds for _, result in requests) * 1.15)
    body = _axis_ticks(maximum_y, chart_height=chart_height, top=top, left=left)
    for strength, result in requests:
        x = left + result.started_offset_seconds / maximum_x * chart_width
        y = top + chart_height - result.duration_seconds / maximum_y * chart_height
        failed = not isinstance(result.status, int) or result.status >= 400
        color = "#ba4434" if failed else "#3154c9" if strength == "standard" else "#8b887f"
        shape = (
            f'<rect x="{x - 3:.1f}" y="{y - 3:.1f}" width="6" height="6" fill="{color}"/>'
            if strength == "thorough"
            else f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>'
        )
        body.append(shape)
    for index in range(5):
        fraction = index / 4
        x = left + fraction * chart_width
        body.append(
            f'<text class="axis" x="{x:.1f}" y="420" text-anchor="middle">'
            f"{fraction * maximum_x:.0f}s</text>"
        )
    body.append('<text class="axis" x="18" y="235" transform="rotate(-90 18 235)">seconds</text>')
    body.append('<circle cx="945" cy="36" r="4" fill="#3154c9"/>')
    body.append('<text class="axis" x="956" y="41">Standard</text>')
    body.append('<rect x="1040" y="32" width="8" height="8" fill="#8b887f"/>')
    body.append('<text class="axis" x="1054" y="41">Thorough</text>')
    return _svg_shell(title, description, "\n".join(body))


def _stage_payload(stage: StageSummary) -> dict[str, object]:
    return {
        "strength": stage.strength,
        "target_rps": stage.target_rps,
        "duration_seconds": stage.duration_seconds,
        "wall_seconds": round(stage.wall_seconds, 6),
        "dropped": stage.dropped,
        "p50_seconds": round(stage.p50_seconds, 6),
        "p95_seconds": round(stage.p95_seconds, 6),
        "max_seconds": round(stage.max_seconds, 6),
        "status_counts": dict(stage.status_counts),
        "requests": [
            {
                "status": result.status,
                "duration_seconds": round(result.duration_seconds, 6),
                "degraded": result.degraded,
                "started_offset_seconds": round(result.started_offset_seconds, 6),
            }
            for result in stage.results
        ],
    }


def _report_html(
    *,
    base_url: str,
    started_at: datetime,
    finished_at: datetime,
    stages: Sequence[StageSummary],
) -> str:
    rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(_stage_label(stage))}</td>"
        f"<td>{len(stage.results)}</td>"
        f"<td>{stage.p50_seconds:.2f}s</td>"
        f"<td>{stage.p95_seconds:.2f}s</td>"
        f"<td>{stage.max_seconds:.2f}s</td>"
        f"<td>{html.escape(str(dict(stage.status_counts)))}</td>"
        "</tr>"
        for stage in stages
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scholight production canary report</title>
<style>
body{{margin:0;background:#f4f2ec;color:#25241f;font-family:Inter,Arial,sans-serif}}
main{{max-width:1240px;margin:0 auto;padding:48px 28px 72px}}
h1,h2{{font-family:Georgia,serif}} h1{{font-size:42px;margin:0 0 8px}}
.meta{{color:#6f6c63;margin-bottom:40px}} section{{margin:36px 0}}
img{{display:block;width:100%;height:auto;border:1px solid #dedbd2;background:#fbfaf6}}
table{{width:100%;border-collapse:collapse;background:#fbfaf6}}
th,td{{padding:12px;text-align:left;border-bottom:1px solid #dedbd2}}
th{{color:#6f6c63;font-size:13px;text-transform:uppercase;letter-spacing:.04em}}
code{{font-family:ui-monospace,monospace}}
</style>
</head>
<body>
<main>
<h1>Scholight production canary</h1>
<p class="meta"><code>{html.escape(base_url)}</code><br>
{html.escape(started_at.isoformat())} to {html.escape(finished_at.isoformat())}</p>
<section><img src="latency-by-stage.svg" alt="Latency by stage chart"></section>
<section><img src="request-timeline.svg" alt="Request latency timeline chart"></section>
<section><img src="outcomes-by-stage.svg" alt="Request outcomes by stage chart"></section>
<section>
<h2>Stage summary</h2>
<table>
<thead><tr><th>Stage</th><th>Requests</th><th>P50</th><th>P95</th><th>Max</th><th>Status</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</section>
</main>
</body>
</html>
"""


def write_report(
    output_dir: Path,
    *,
    base_url: str,
    started_at: datetime,
    finished_at: datetime,
    stages: Sequence[StageSummary],
) -> tuple[Path, ...]:
    """Write machine-readable results and three dependency-free SVG charts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "base_url": base_url,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
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
                        "duration_seconds": f"{result.duration_seconds:.6f}",
                        "degraded": result.degraded,
                        "started_offset_seconds": f"{result.started_offset_seconds:.6f}",
                    }
                )

    latency_path = output_dir / "latency-by-stage.svg"
    latency_path.write_text(_latency_chart(stages), encoding="utf-8")
    outcomes_path = output_dir / "outcomes-by-stage.svg"
    outcomes_path.write_text(_outcomes_chart(stages), encoding="utf-8")
    timeline_path = output_dir / "request-timeline.svg"
    timeline_path.write_text(_timeline_chart(stages), encoding="utf-8")
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
    return (
        report_path,
        results_path,
        csv_path,
        latency_path,
        timeline_path,
        outcomes_path,
    )


async def _request(
    client: httpx.AsyncClient,
    *,
    url: str,
    api_key: str,
    query: str,
    strength: str,
    started_offset_seconds: float,
    abort_event: asyncio.Event,
) -> RequestResult:
    started = time.perf_counter()
    try:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": query, "strength": strength, "limit": 5},
        )
        degraded = False
        if response.headers.get("content-type", "").startswith("application/json"):
            payload = response.json()
            if isinstance(payload, dict):
                degraded = payload.get("degraded") is True
        if response.status_code >= 500:
            abort_event.set()
        return RequestResult(
            status=response.status_code,
            duration_seconds=time.perf_counter() - started,
            degraded=degraded,
            started_offset_seconds=started_offset_seconds,
        )
    except httpx.HTTPError as exc:
        abort_event.set()
        return RequestResult(
            status=type(exc).__name__,
            duration_seconds=time.perf_counter() - started,
            degraded=False,
            started_offset_seconds=started_offset_seconds,
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
    """Run one bounded constant-arrival-rate stage."""
    summary = StageSummary(
        strength=strength,
        target_rps=target_rps,
        duration_seconds=duration_seconds,
    )
    abort_event = asyncio.Event()
    pending: set[asyncio.Task[RequestResult]] = set()
    request_count = max(1, round(target_rps * duration_seconds))
    started = time.perf_counter()

    for index in range(request_count):
        due_at = started + index / target_rps
        await asyncio.sleep(max(0.0, due_at - time.perf_counter()))
        if abort_event.is_set():
            break
        pending = {task for task in pending if not task.done()}
        if len(pending) >= _MAX_IN_FLIGHT:
            summary.dropped += 1
            abort_event.set()
            break
        task = asyncio.create_task(
            _request(
                client,
                url=url,
                api_key=api_key,
                query=_QUERIES[index % len(_QUERIES)],
                strength=strength,
                started_offset_seconds=time.perf_counter() - run_started,
                abort_event=abort_event,
            )
        )
        pending.add(task)
        task.add_done_callback(lambda completed: summary.results.append(completed.result()))

    if pending:
        await asyncio.gather(*pending)
    summary.wall_seconds = time.perf_counter() - started
    return summary


def _print_summary(summary: StageSummary) -> None:
    degraded = sum(result.degraded for result in summary.results)
    achieved = len(summary.results) / summary.wall_seconds if summary.wall_seconds else 0.0
    print(
        f"{summary.strength} target={summary.target_rps:g}rps "
        f"requests={len(summary.results)} status={dict(summary.status_counts)} "
        f"achieved={achieved:.2f}rps p50={summary.p50_seconds:.2f}s "
        f"p95={summary.p95_seconds:.2f}s max={summary.max_seconds:.2f}s "
        f"degraded={degraded} dropped={summary.dropped}"
    )


async def run_canary(
    *,
    base_url: str,
    api_key: str,
    stage_seconds: int,
    maximum_standard_rps: float,
    maximum_thorough_rps: float | None,
) -> tuple[int, datetime, datetime, list[StageSummary]]:
    """Run all requested stages and stop at the first breached boundary."""
    started_at = datetime.now(UTC)
    run_started = time.perf_counter()
    summaries: list[StageSummary] = []
    timeout = httpx.Timeout(90.0, connect=10.0)
    limits = httpx.Limits(max_connections=_MAX_IN_FLIGHT, max_keepalive_connections=16)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        stage_specs = [
            ("standard", rate, 10.0)
            for rate in build_rate_plan(
                maximum_standard_rps,
                candidates=_STANDARD_RATE_CANDIDATES,
            )
        ]
        if maximum_thorough_rps is not None:
            stage_specs.extend(
                ("thorough", rate, 45.0)
                for rate in build_rate_plan(
                    maximum_thorough_rps,
                    candidates=_THOROUGH_RATE_CANDIDATES,
                )
            )

        for strength, rate, p95_limit in stage_specs:
            summary = await run_stage(
                client,
                url=f"{base_url}/api/search",
                api_key=api_key,
                strength=strength,
                target_rps=rate,
                duration_seconds=stage_seconds,
                run_started=run_started,
            )
            summaries.append(summary)
            _print_summary(summary)
            if reason := evaluate_stage(summary, p95_limit_seconds=p95_limit):
                print(f"STOP: {reason}")
                return 1, started_at, datetime.now(UTC), summaries
    return 0, started_at, datetime.now(UTC), summaries


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        msg = "value must be greater than zero"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Scholight origin without /api")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Acknowledge that the target is not loopback",
    )
    parser.add_argument(
        "--stage-seconds",
        type=int,
        default=20,
        choices=range(5, _MAX_STAGE_SECONDS + 1),
        metavar="5..120",
    )
    parser.add_argument(
        "--max-standard-rps",
        type=_positive_float,
        default=4.0,
        help=f"maximum Standard arrival rate, hard limit {_MAX_STANDARD_RPS:g}",
    )
    parser.add_argument(
        "--max-thorough-rps",
        type=_positive_float,
        help=f"include Thorough stages up to this rate, hard limit {_MAX_THOROUGH_RPS:g}",
    )
    parser.add_argument(
        "--report-root",
        type=Path,
        default=Path("data/load-tests"),
        help="directory under which a timestamped report is written",
    )
    return parser.parse_args()


def main(env: Mapping[str, str] = os.environ) -> int:
    """CLI entry point."""
    args = _parse_args()
    try:
        base_url = validate_target(args.base_url, allow_remote=args.allow_remote)
    except ValueError as exc:
        print(f"configuration error: {exc}")
        return 2
    if args.max_standard_rps > _MAX_STANDARD_RPS:
        print(f"configuration error: --max-standard-rps cannot exceed {_MAX_STANDARD_RPS:g}")
        return 2
    if args.max_thorough_rps is not None and args.max_thorough_rps > _MAX_THOROUGH_RPS:
        print(f"configuration error: --max-thorough-rps cannot exceed {_MAX_THOROUGH_RPS:g}")
        return 2
    api_key = env.get(_API_KEY_ENV, "")
    if not api_key.startswith("sk_live_"):
        print(f"configuration error: {_API_KEY_ENV} must contain an Access Key")
        return 2
    exit_code, started_at, finished_at, stages = asyncio.run(
        run_canary(
            base_url=base_url,
            api_key=api_key,
            stage_seconds=args.stage_seconds,
            maximum_standard_rps=args.max_standard_rps,
            maximum_thorough_rps=args.max_thorough_rps,
        )
    )
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
