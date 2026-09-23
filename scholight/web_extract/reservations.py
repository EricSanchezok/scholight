"""Conservative stage envelopes owned by execution, never by queue waiters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from scholight.logging.emf import emit_emf
from scholight.web_extract.admission import capacity_error

MIB = 1024 * 1024
Stage = Literal["download", "parse", "browser"]


@dataclass(frozen=True, slots=True)
class StageCost:
    fixed: int
    per_input_byte: int

    def estimate(self, size: int) -> int:
        return self.fixed + self.per_input_byte * size


@dataclass(frozen=True, slots=True)
class MemoryModel:
    # Calibration must validate these conservative envelopes before release.
    download: StageCost = StageCost(8 * MIB, 1)
    html: StageCost = StageCost(32 * MIB, 64)
    pdf: StageCost = StageCost(64 * MIB, 128)
    text: StageCost = StageCost(8 * MIB, 8)
    browser: StageCost = StageCost(192 * MIB, 0)

    def estimate(self, stage: Stage, size: int, mime: str) -> int:
        if size < 0:
            raise ValueError("Input size must be nonnegative")
        mime = mime.partition(";")[0].strip().lower()
        if stage == "download":
            cost = self.download
        elif stage == "browser":
            cost = self.browser
        elif mime in {"text/html", "application/xhtml+xml"}:
            cost = self.html
        elif mime == "application/pdf":
            cost = self.pdf
        elif mime.startswith("text/") or mime in {"application/json", "application/xml"}:
            cost = self.text
        else:
            # Content sniffing can discover a PDF behind an unhelpful MIME.
            cost = self.pdf
        return cost.estimate(size)


class MemoryBudget:
    def __init__(
        self,
        sample: Callable[[], int],
        admit: Callable[[], None],
        *,
        model: MemoryModel | None = None,
        high: int = 640 * MIB,
    ) -> None:
        self._sample = sample
        self._admit = admit
        self._model = model or MemoryModel()
        self._high = high
        self.reserved_bytes = 0

    def lease(self) -> MemoryReservation:
        return MemoryReservation(self)

    def transfer(self, old: int, *, stage: Stage, size: int, mime: str) -> int:
        self._admit()
        new = self._model.estimate(stage, size, mime)
        try:
            working = self._sample()
        except (OSError, ValueError, KeyError) as error:
            raise capacity_error() from error
        reserved = self.reserved_bytes - old + new
        if working + reserved > self._high:
            emit_emf(service="extract", metrics={"MemoryReservationRejected": (1, "Count")})
            raise capacity_error()
        self.reserved_bytes = reserved
        return new

    def release(self, amount: int) -> None:
        self.reserved_bytes -= amount


class MemoryReservation:
    def __init__(self, budget: MemoryBudget) -> None:
        self._budget = budget
        self._amount = 0
        self._closed = False

    def transfer(self, stage: Stage, *, size: int = 0, mime: str = "") -> None:
        if self._closed:
            raise RuntimeError("Cannot transfer a closed reservation")
        self._amount = self._budget.transfer(self._amount, stage=stage, size=size, mime=mime)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._budget.release(self._amount)
