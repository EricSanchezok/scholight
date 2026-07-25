"""Lifecycle-owned thread pool for blocking search SDK calls."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import ParamSpec, TypeVar

from scholight.config import settings

P = ParamSpec("P")
T = TypeVar("T")
_executor: ThreadPoolExecutor | None = None


def start_search_executor() -> None:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=settings.search_executor_workers,
            thread_name_prefix="scholight-search",
        )


def stop_search_executor() -> None:
    global _executor
    executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)


async def run_search_blocking(function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Run a blocking SDK call on the bounded search executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, partial(function, *args, **kwargs))


__all__ = ["run_search_blocking", "start_search_executor", "stop_search_executor"]
