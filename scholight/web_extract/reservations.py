"""Conservative stage envelopes owned by execution, never by queue waiters."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from scholight.logging.emf import emit_emf
from scholight.web_extract.admission import capacity_error
from scholight.web_extract.errors import ExtractError

MIB = 1024 * 1024
Stage = Literal["download", "parse", "browser"]
WorkerKind = Literal["parser", "browser"]


class RetainedMemoryPressureError(ExtractError):
    """An execution envelope may fit after retiring idle resident heaps."""

    def __init__(self) -> None:
        error = capacity_error()
        super().__init__(
            code=error.code,
            message=error.message,
            status_code=error.status_code,
            retryable=error.retryable,
            retry_after=error.retry_after,
        )


@dataclass(frozen=True, slots=True)
class StageCost:
    fixed: int
    per_input_byte: int

    def estimate(self, size: int) -> int:
        return self.fixed + self.per_input_byte * size


@dataclass(frozen=True, slots=True)
class MemoryModel:
    # Five native ARM64 rounds, observed peak deltas + 50% and 8 MiB fixed margin.
    # Final combined-load validation remains a release gate; see extract-runtime.md.
    download: StageCost = StageCost(11 * MIB, 1)
    html: StageCost = StageCost(63 * MIB, 106)
    pdf: StageCost = StageCost(309 * MIB, 1)
    text: StageCost = StageCost(25 * MIB, 1)
    browser: StageCost = StageCost(253 * MIB, 0)
    parser_startup: int = 224 * MIB
    browser_startup: int = 265 * MIB

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
        on_pressure: Callable[[], None] | None = None,
    ) -> None:
        self._sample = sample
        self._admit = admit
        self._model = model or MemoryModel()
        self._high = high
        self._on_pressure = on_pressure or (lambda: None)
        self.reserved_bytes = 0

    def lease(self) -> MemoryReservation:
        return MemoryReservation(self)

    def transfer(self, old: int, *, stage: Stage, size: int, mime: str) -> int:
        return self._replace(old, self._model.estimate(stage, size, mime))

    @contextmanager
    def startup(self, kind: WorkerKind) -> Iterator[None]:
        amount = self._replace(
            0,
            self._model.parser_startup if kind == "parser" else self._model.browser_startup,
            startup=True,
        )
        try:
            yield
        finally:
            self.release(amount)

    def _replace(self, old: int, new: int, *, startup: bool = False) -> int:
        self._admit()
        try:
            working = self._sample()
        except (OSError, ValueError, KeyError) as error:
            raise capacity_error() from error
        reserved = self.reserved_bytes - old + new
        if working + reserved > self._high:
            emit_emf(service="extract", metrics={"MemoryReservationRejected": (1, "Count")})
            if (working + new > self._high or startup) and new <= self._high:
                # Reclaim retained heaps when idle; competing reservations and
                # intrinsically oversized jobs alone do not recycle warm workers.
                # A cold generation also needs room for its retained input.
                self._on_pressure()
                raise RetainedMemoryPressureError
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
