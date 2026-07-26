"""Passive queue-wait observation for blocking search SDK calls."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from functools import partial
from typing import ParamSpec, TypeVar

from scholight.logging.emf import emit_emf

P = ParamSpec("P")
T = TypeVar("T")


async def run_search_blocking(function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Use asyncio's normal executor while measuring time spent waiting to start."""
    scheduled_at = time.perf_counter()

    def invoke() -> T:
        wait_ms = max(0.0, (time.perf_counter() - scheduled_at) * 1000)
        emit_emf(
            service="api",
            metrics={"ThreadPoolWait": (wait_ms, "Milliseconds")},
        )
        return partial(function, *args, **kwargs)()

    return await asyncio.to_thread(invoke)


__all__ = ["run_search_blocking"]
