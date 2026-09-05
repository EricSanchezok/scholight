"""Small durable-DAG primitives for resumable Survey stages and shards."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal


class SurveyArtifactContractError(Exception):
    """A unit returned without producing its declared durable artifact."""


@dataclass(frozen=True, slots=True)
class ArtifactContract:
    path: str
    kind: Literal["file", "json"] = "file"
    maximum_bytes: int = 16 * 1024 * 1024

    def validate(self, run_root: Path) -> None:
        relative = PurePosixPath(self.path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise SurveyArtifactContractError(f"Unsafe artifact path: {self.path}")
        target = run_root.joinpath(*relative.parts)
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise SurveyArtifactContractError(f"Required artifact is missing: {self.path}") from exc
        if not target.is_file() or size <= 0 or size > self.maximum_bytes:
            raise SurveyArtifactContractError(f"Required artifact is invalid: {self.path}")
        if self.kind == "json":
            try:
                value = json.loads(target.read_bytes())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SurveyArtifactContractError(
                    f"Required JSON artifact is invalid: {self.path}"
                ) from exc
            if not isinstance(value, (dict, list)):
                raise SurveyArtifactContractError(f"Required JSON artifact is invalid: {self.path}")


@dataclass(frozen=True, slots=True)
class DurableUnit:
    name: str
    workflow: str
    purpose: str
    artifacts: tuple[ArtifactContract, ...]


RunUnit = Callable[[DurableUnit], Awaitable[None]]
CheckpointUnit = Callable[[str, tuple[str, ...]], Awaitable[None]]


class DurableSurveyExecutor:
    """Validate, checkpoint, then expose completion to downstream units."""

    def __init__(
        self,
        *,
        run_root: Path,
        completed_units: Sequence[str],
        run_unit: RunUnit,
        checkpoint: CheckpointUnit,
    ) -> None:
        self._run_root = run_root
        self._completed = set(completed_units)
        self._run_unit = run_unit
        self._checkpoint = checkpoint
        self._commit_lock = asyncio.Lock()

    @property
    def completed_units(self) -> tuple[str, ...]:
        return tuple(sorted(self._completed))

    async def execute(self, unit: DurableUnit) -> None:
        if unit.name in self._completed:
            self._validate(unit)
            return
        await self._run_unit(unit)
        async with self._commit_lock:
            if unit.name in self._completed:
                self._validate(unit)
                return
            self._validate(unit)
            completed = tuple(sorted((*self._completed, unit.name)))
            await self._checkpoint(unit.name, completed)
            self._completed.add(unit.name)

    async def execute_many(
        self,
        units: Sequence[DurableUnit],
        *,
        concurrency: int,
    ) -> None:
        if concurrency < 1:
            raise ValueError("Survey unit concurrency must be positive")
        names = [unit.name for unit in units]
        if len(names) != len(set(names)):
            raise ValueError("Survey durable unit names must be unique")
        semaphore = asyncio.Semaphore(concurrency)

        async def _bounded(unit: DurableUnit) -> None:
            async with semaphore:
                await self.execute(unit)

        results = await asyncio.gather(
            *(_bounded(unit) for unit in units),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    def _validate(self, unit: DurableUnit) -> None:
        if not unit.artifacts:
            raise SurveyArtifactContractError(f"Durable unit declares no artifacts: {unit.name}")
        for contract in unit.artifacts:
            contract.validate(self._run_root)


__all__ = [
    "ArtifactContract",
    "DurableSurveyExecutor",
    "DurableUnit",
    "SurveyArtifactContractError",
]
