"""N-1 query adapter: an explicit destination selects only its isolated queue.

The legacy SQL remains unchanged for existing deployments. Retire this adapter's
legacy branch after all full-ingestion consumers adopt verified target baselines.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from inspect import signature
from typing import ParamSpec, TypeVar, cast

from scholight.config import settings
from scholight.db import queries_ingestion as legacy
from scholight.db.client import DBError
from scholight.db.queries_ingestion import (
    IngestionJob,
    get_sync_state,
    initialize_sync_cursor,
    mark_sync_failed,
    mark_sync_started,
    mark_sync_succeeded,
)
from scholight.db.target_ingestion import TargetQueue
from scholight.models.ingestion_target import IngestionTarget
from scholight.store.client import get_client

__all__ = [
    "IngestionJob",
    "claim_ingestion_job",
    "complete_ingestion_job",
    "enqueue_ingestion_job",
    "fail_ingestion_job",
    "get_ingestion_job",
    "get_ingestion_status",
    "get_sync_state",
    "initialize_sync_cursor",
    "mark_sync_failed",
    "mark_sync_started",
    "mark_sync_succeeded",
    "release_ingestion_job",
    "renew_ingestion_job_lease",
    "retry_ingestion_job",
    "configured_queue",
    "verified_sync_source",
]
P = ParamSpec("P")
T = TypeVar("T")


def configured_queue() -> TargetQueue | None:
    return TargetQueue(settings.ingestion_target_id) if settings.ingestion_target_id else None


def _destination(method: str) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    def decorate(previous: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        contract = signature(previous)

        @wraps(previous)
        async def dispatch(*args: P.args, **kwargs: P.kwargs) -> T:
            queue = configured_queue()
            if queue is None:
                return await cast("Callable[P, Awaitable[T]]", getattr(legacy, previous.__name__))(
                    *args, **kwargs
                )
            bound = contract.bind(*args, **kwargs)
            operation = cast("Callable[..., Awaitable[T]]", getattr(queue, method))
            return await operation(*bound.args, **bound.kwargs)

        return dispatch

    return decorate


enqueue_ingestion_job = _destination("enqueue")(legacy.enqueue_ingestion_job)
claim_ingestion_job = _destination("claim")(legacy.claim_ingestion_job)
renew_ingestion_job_lease = _destination("renew")(legacy.renew_ingestion_job_lease)
release_ingestion_job = _destination("release")(legacy.release_ingestion_job)
complete_ingestion_job = _destination("complete")(legacy.complete_ingestion_job)
fail_ingestion_job = _destination("fail")(legacy.fail_ingestion_job)
retry_ingestion_job = _destination("retry")(legacy.retry_ingestion_job)
get_ingestion_job = _destination("get")(legacy.get_ingestion_job)
get_ingestion_status = _destination("status")(legacy.get_ingestion_status)


def _actual_target(
    *,
    deadline: float | None = None,
    rpc_timeout: float | None = None,
    cancelled: threading.Event | None = None,
) -> IngestionTarget:
    def remaining() -> float | None:
        if cancelled is not None and cancelled.is_set():
            raise TimeoutError("Destination verification was cancelled")
        if deadline is None:
            return rpc_timeout
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("Destination verification exceeded its deadline")
        return min(rpc_timeout, budget) if rpc_timeout is not None else budget

    timeout = remaining()
    if timeout is None:
        client = get_client()
    elif deadline is None:
        client = get_client(timeout=timeout)
    else:
        client = get_client(timeout=timeout, deadline=deadline)
    ids = []
    for name in ("arxiv_papers", "arxiv_chunks"):
        timeout = remaining()
        options = {} if timeout is None else {"timeout": timeout}
        ids.append(str(client.describe_collection(name, **options)["collection_id"]))
    remaining()
    return IngestionTarget(
        settings.zilliz_uri.rstrip("/"),
        ids[0],
        ids[1],
        settings.embedding_model,
        settings.embedding_dim,
    )


async def verified_sync_source(
    *, deadline: float | None = None, rpc_timeout: float | None = None
) -> str:
    queue = configured_queue()
    if queue is None:
        return "arxiv"
    if deadline is None and rpc_timeout is None:
        actual = await asyncio.to_thread(_actual_target)
    else:
        cancelled = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                _actual_target, deadline=deadline, rpc_timeout=rpc_timeout, cancelled=cancelled
            )
        )
        try:
            actual = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            # A cancelled to_thread await does not stop its RPC. Join the bounded
            # read before propagating cancellation; never start an overlapping retry.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise
    if actual.key != queue.target_id:
        raise DBError("Actual collection identity does not match the ingestion target")
    return await queue.sync_source()
