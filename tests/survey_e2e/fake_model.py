"""Deterministic OpenAI-compatible model used only by the hermetic Survey E2E."""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request

app = FastAPI()


def _tool_names(body: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for tool in body.get("tools", []):
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.append(function["name"])
    return names


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{uuid4().hex}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def _response(message: dict[str, Any], *, finish_reason: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "e2e-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
    }


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/{path:path}")
async def completion(request: Request, path: str) -> dict[str, Any]:
    del path
    body = await request.json()
    names = _tool_names(body)
    messages = body.get("messages", [])
    tool_results = [
        message
        for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"
    ]
    search_name = next((name for name in names if name.endswith("search_papers")), None)
    if search_name is not None and not tool_results:
        return _response(
            _tool_call(
                search_name,
                {
                    "query": "retrieval augmented generation evaluation",
                    "strength": "standard",
                    "limit": 1,
                },
            ),
            finish_reason="tool_calls",
        )
    if "fs" in names and len(tool_results) == 1:
        return _response(
            _tool_call(
                "fs",
                {
                    "action": "write",
                    "filePath": "08_survey.md",
                    "content": (
                        "# Retrieval-Augmented Generation Survey\n\n"
                        "This deterministic report verifies the complete Survey runtime.\n"
                    ),
                },
            ),
            finish_reason="tool_calls",
        )
    content = (
        "# Research requirement\n\n"
        "Evaluate retrieval-augmented generation methods, evidence quality, and benchmarks."
        if "fs" not in names
        else "The Survey report was written successfully."
    )
    return _response({"role": "assistant", "content": content}, finish_reason="stop")
