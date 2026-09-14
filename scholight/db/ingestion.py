"""N-1 query adapter: an explicit destination selects only its isolated queue.

The legacy SQL remains unchanged for existing deployments. Retire this adapter's
legacy branch after all full-ingestion consumers adopt verified target baselines.
"""

from __future__ import annotations

import asyncio
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


def _actual_target() -> IngestionTarget:
    client = get_client()
    ids = [
        str(client.describe_collection(name)["collection_id"])
        for name in ("arxiv_papers", "arxiv_chunks")
    ]
    return IngestionTarget(
        settings.zilliz_uri.rstrip("/"),
        ids[0],
        ids[1],
        settings.embedding_model,
        settings.embedding_dim,
    )


async def verified_sync_source() -> str:
    queue = configured_queue()
    if queue is None:
        return "arxiv"
    actual = await asyncio.to_thread(_actual_target)
    if actual.key != queue.target_id:
        raise DBError("Actual collection identity does not match the ingestion target")
    return await queue.sync_source()
