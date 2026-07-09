"""Abstract base classes for composable search pipelines.

A ``Pipeline`` is a sequence of ``Phase`` steps that each mutate a shared
``PipelineContext``.  This design keeps the framework simple — phases read
and write the context bag directly rather than chaining typed outputs.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

from scholight.models.search import SearchRequest

logger = structlog.get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Shared context bag
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class PipelineContext:
    """Mutable bag of state threaded through every phase of a pipeline.

    Phases read from and write to this context.  Key fields:

    * ``request`` — the original ``SearchRequest`` (always present).
    * ``query_vector`` — set by ``EmbedPhase``.
    * ``raw_hits`` — list of raw hit dicts; phases append / reorder.
    * ``chunk_hits`` — a second hit list used by Level2 chunk phases.
    * ``timings`` — phase-name → milliseconds map.
    * ``metadata`` — arbitrary key-value annotations for stats / reporting.
    """

    request: SearchRequest
    query_vector: list[float] | None = None
    sparse_vector: dict[int, float] | None = None
    raw_hits: list[dict[str, Any]] = field(default_factory=list)
    chunk_hits: list[dict[str, Any]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def record_timing(self, phase_name: str, duration_ms: float) -> None:
        self.timings[phase_name] = duration_ms


# ══════════════════════════════════════════════════════════════════════════════
# Phase
# ══════════════════════════════════════════════════════════════════════════════


class Phase(ABC):
    """One step in a search pipeline.

    Subclasses implement ``execute(ctx)``, which mutates *ctx* in place.
    The ``__call__`` wrapper records timing and wraps exceptions in
    ``PhaseError`` for consistent diagnostics.
    """

    name: str = ""  # set by subclass; used as timing key

    @abstractmethod
    async def execute(self, ctx: PipelineContext) -> None:
        """Mutate *ctx* in place.  Raise on unrecoverable failure."""
        ...

    async def __call__(self, ctx: PipelineContext) -> None:
        t0 = time.perf_counter()
        try:
            await self.execute(ctx)
        except Exception as exc:
            raise PhaseError(self.name, exc) from exc
        finally:
            ctx.record_timing(self.name, (time.perf_counter() - t0) * 1000)


class PhaseError(Exception):
    """Wraps an exception raised inside a ``Phase.execute()``."""

    def __init__(self, phase_name: str, cause: Exception) -> None:
        self.phase_name = phase_name
        self.cause = cause
        super().__init__(f"Phase '{phase_name}' failed: {cause}")


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline
# ══════════════════════════════════════════════════════════════════════════════


class Pipeline(ABC):
    """A sequence of ``Phase`` steps.

    ``run(request)`` creates a fresh ``PipelineContext``, runs every phase
    in order, and returns the completed context.  Subclasses typically just
    list their phases in ``__init__``.
    """

    phases: list[Phase]

    async def run(
        self, request: SearchRequest, ctx: PipelineContext | None = None
    ) -> PipelineContext:
        if ctx is None:
            ctx = PipelineContext(request=request)
        for phase in self.phases:
            await phase(ctx)
        return ctx
