"""Benchmark registry — maps benchmark names to their data/runner config."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_DATA = PROJECT_ROOT / "benchmark"


@dataclass(frozen=True)
class BenchmarkSpec:
    """Static metadata for one benchmark under the registry."""

    key: str
    name: str
    data_dir: Path
    types: tuple[str, ...] = ("selection",)
    runner_module: str = ""
    runner_class: str = ""

    @property
    def output_root(self) -> Path:
        return PROJECT_ROOT / "data" / "benchmark" / self.key


_REGISTRY: dict[str, BenchmarkSpec] = {}


def _register(spec: BenchmarkSpec) -> BenchmarkSpec:
    _REGISTRY[spec.key] = spec
    return spec


# ── Registered benchmarks ────────────────────────────────────────────────

_register(
    BenchmarkSpec(
        key="autoresearchbench",
        name="AutoResearchBench",
        data_dir=BENCHMARK_DATA / "autoresearchbench",
        types=("wide", "deep"),
        runner_module="runners.auto_research_bench",
        runner_class="AutoResearchBenchRunner",
    )
)

_register(
    BenchmarkSpec(
        key="scholargym",
        name="ScholarGym",
        data_dir=BENCHMARK_DATA / "scholargym",
        types=("selection",),
        runner_module="runners.scholar_gym",
        runner_class="ScholarGymRunner",
    )
)


def list_specs() -> list[BenchmarkSpec]:
    return sorted(_REGISTRY.values(), key=lambda s: s.key)


def get_spec(key: str) -> BenchmarkSpec:
    spec = _REGISTRY.get(key)
    if spec is None:
        raise KeyError(f"Unknown benchmark: {key!r}.  Available: {sorted(_REGISTRY)}")
    return spec
