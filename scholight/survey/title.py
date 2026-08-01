"""Fast, best-effort navigation titles for newly created Surveys."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from scholight.config import settings

logger = structlog.get_logger(__name__)

_MODEL = "deepseek-v4-flash"
_TITLE_MAX_CHARACTERS = 80
_SYSTEM_PROMPT = """SCHOLIGHT_SURVEY_NAVIGATION_TITLE
Generate exactly one concise navigation title for the research request.
Preserve the user's primary language. Capture the research subject and comparison when present.
Return plain text only: no Markdown, labels, quotation marks, explanation, or final punctuation.
Do not answer the research request. Keep the title under 80 characters."""


def _normalize_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    title = next((line.strip() for line in value.splitlines() if line.strip()), "")
    title = title.removeprefix("#").strip().strip("`\"'“”\u2018\u2019")
    title = " ".join(title.split())
    if not title:
        return None
    if len(title) > _TITLE_MAX_CHARACTERS:
        title = f"{title[: _TITLE_MAX_CHARACTERS - 1].rstrip()}…"
    return title


def _response_title(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    return _normalize_title(message.get("content"))


async def generate_survey_title(
    initial_request: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Generate one title without making Survey creation depend on provider availability."""
    if not settings.deepseek_api_key:
        logger.warning("survey_title_generation_skipped", reason="missing_api_key")
        return None
    request: dict[str, Any] = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": initial_request},
        ],
        "temperature": 0.2,
        "max_tokens": 64,
        "stream": False,
    }
    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(connect=1.0, read=3.0, write=2.0, pool=1.0),
        follow_redirects=False,
    )
    try:
        response = await http_client.post(
            settings.survey_title_api_url,
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            json=request,
        )
        response.raise_for_status()
        title = _response_title(response.json())
        if title is None:
            logger.warning("survey_title_invalid_response")
        return title
    except (httpx.HTTPError, ValueError) as exc:
        status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        logger.warning(
            "survey_title_generation_failed",
            error_type=type(exc).__name__,
            status_code=status_code,
        )
        return None
    finally:
        if owns_client:
            await http_client.aclose()


__all__ = ["generate_survey_title"]
