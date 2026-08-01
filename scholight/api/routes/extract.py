"""Authenticated public Web Extract endpoint."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from structlog.contextvars import get_contextvars

from scholight.api.deps import SearchActor, get_extract_actor
from scholight.api.extract_execution import (
    ExtractInvocation,
    PublicExtractError,
    execute_public_extract,
)
from scholight.models.web_extract import ExtractRequest, ExtractResponse

router = APIRouter()


@router.post("", response_model=ExtractResponse)
async def extract(
    request: Request,
    body: ExtractRequest,
    actor: SearchActor = Depends(get_extract_actor),
) -> ExtractResponse:
    request_id = str(get_contextvars().get("request_id") or uuid4())
    try:
        return await execute_public_extract(
            body,
            ExtractInvocation(actor=actor, request_id=request_id, transport="rest"),
        )
    except PublicExtractError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        if exc.status_code == 401:
            headers = {**(headers or {}), "WWW-Authenticate": "Bearer"}
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.http_detail,
            headers=headers,
        ) from exc


__all__ = ["router"]
