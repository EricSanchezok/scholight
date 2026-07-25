"""Run a bounded, progressively increasing search canary against Scholight."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import math
import os
import time
from collections import Counter
from dataclasses import dataclass, field
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


async def _request(
    client: httpx.AsyncClient,
    *,
    url: str,
    api_key: str,
    query: str,
    strength: str,
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
        )
    except httpx.HTTPError as exc:
        abort_event.set()
        return RequestResult(
            status=type(exc).__name__,
            duration_seconds=time.perf_counter() - started,
            degraded=False,
        )


async def run_stage(
    client: httpx.AsyncClient,
    *,
    url: str,
    api_key: str,
    strength: str,
    target_rps: float,
    duration_seconds: int,
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
) -> int:
    """Run all requested stages and stop at the first breached boundary."""
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
            )
            _print_summary(summary)
            if reason := evaluate_stage(summary, p95_limit_seconds=p95_limit):
                print(f"STOP: {reason}")
                return 1
    return 0


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
    return asyncio.run(
        run_canary(
            base_url=base_url,
            api_key=api_key,
            stage_seconds=args.stage_seconds,
            maximum_standard_rps=args.max_standard_rps,
            maximum_thorough_rps=args.max_thorough_rps,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
