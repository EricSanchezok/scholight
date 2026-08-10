"""Deterministic OpenAI-compatible model used only by the hermetic Survey E2E."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from itertools import pairwise
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
_REQUEST_COUNTS: Counter[str] = Counter()

_STAGE_MARKERS = {
    "AnchorAgent": "anchor",
    "QueryPlanner": "query_plan",
    "MethodScout": "method_scout",
    "BenchmarkScout": "benchmark_scout",
    "SurveyScout": "survey_scout",
    "FrontierScout": "frontier_scout",
    "DiscoveryMerger": "discovery_merger",
    "CitationSeedSelector": "citation_seed_selector",
    "generates a list of expanded references": "reference_expander",
    "SemanticExpander": "semantic_expander",
    "CrossDomainExpander": "cross_domain_expander",
    "ExpansionMerger": "expansion_merger",
    "DedupRanker": "rank_pool",
    "CardPlanner": "card_plan",
    "PaperCardWriter": "paper_card",
    "ResearchMapBuilder": "research_map",
    "CoverageJudge": "coverage_judge",
    "ScopeJudge": "scope_judge",
    "BenchmarkJudge": "benchmark_judge",
    "GapJudge": "gap_judge",
    "JudgeSynthesizer": "judge_synthesizer",
    "ImagePlanner": "image_planner",
    "SurveyOutline author": "survey_outline",
    "SectionExpander": "section_expander",
}
_ARTIFACTS = {
    "anchor": "00_survey_spec.md",
    "query_plan": "01_query_plan.md",
    "method_scout": "02a_method_candidates.md",
    "benchmark_scout": "02b_benchmark_candidates.md",
    "survey_scout": "02c_survey_candidates.md",
    "frontier_scout": "02d_frontier_candidates.md",
    "discovery_merger": "02_candidate_pool.md",
    "citation_seed_selector": "03a_seed_papers.md",
    "reference_expander": "03b_citation_expansion.md",
    "semantic_expander": "03c_semantic_expansion.md",
    "cross_domain_expander": "03d_cross_domain.md",
    "expansion_merger": "03_expansion.md",
    "rank_pool": "04_ranked_pool.md",
    "paper_card": "cards/2401.12345.md",
    "research_map": "05_research_map.md",
    "coverage_judge": "06a_coverage_judge.md",
    "scope_judge": "06b_scope_judge.md",
    "benchmark_judge": "06c_benchmark_judge.md",
    "gap_judge": "06d_gap_judge.md",
    "judge_synthesizer": "06_judge_panel.md",
    "survey_outline": "00_outline.md",
    "section_expander": "sections/01_introduction.md",
}
_SEARCH_STAGES = {
    "method_scout",
    "benchmark_scout",
    "survey_scout",
    "frontier_scout",
    "reference_expander",
    "semantic_expander",
    "cross_domain_expander",
}


def _tool_names(body: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for tool in body.get("tools", []):
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.append(function["name"])
    return names


def _called_tools(messages: object) -> list[tuple[str, dict[str, Any]]]:
    called: list[tuple[str, dict[str, Any]]] = []
    if not isinstance(messages, list):
        return called
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls", []):
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                continue
            function = call["function"]
            name = function.get("name")
            arguments = function.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, str):
                continue
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                parsed = {}
            called.append((name, parsed if isinstance(parsed, dict) else {}))
    return called


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
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
    }


def _stage(body: dict[str, Any]) -> str | None:
    messages = body.get("messages", [])
    current_system_prompt = json.dumps(
        [
            message
            for message in messages
            if isinstance(message, dict) and message.get("role") == "system"
        ],
        ensure_ascii=False,
    )
    return next(
        (stage for marker, stage in _STAGE_MARKERS.items() if marker in current_system_prompt),
        None,
    )


def _tool_with_suffix(names: list[str], suffix: str) -> str | None:
    return next((name for name in names if name.casefold().endswith(suffix.casefold())), None)


def _artifact_content(stage: str, path: str) -> str:
    if stage == "anchor":
        return "# Survey specification\n\nEvaluate retrieval-augmented generation evidence.\n"
    if stage == "query_plan":
        return "# Query plan\n\n- retrieval augmented generation evaluation\n"
    if stage in {"method_scout", "benchmark_scout", "survey_scout", "frontier_scout"}:
        return f"# {stage}\n\n- 2401.12345 — Deterministic RAG Evaluation\n"
    if stage == "discovery_merger":
        return "# Candidate pool\n\n- 2401.12345 — Deterministic RAG Evaluation\n"
    if stage == "citation_seed_selector":
        return "# Seed papers\n\n- 2401.12345 — deterministic seed\n"
    if stage in {"reference_expander", "semantic_expander", "cross_domain_expander"}:
        return f"# {stage}\n\nNo additional deterministic candidates.\n"
    if stage == "expansion_merger":
        return "# Expansion\n\nCandidate 2401.12345 remains in scope.\n"
    if stage == "rank_pool":
        return "# Ranked pool\n\n1. 2401.12345 — Deterministic RAG Evaluation — core_set\n"
    if stage == "paper_card":
        return (
            "# PaperCard — 2401.12345\n\n"
            "## header\n\n"
            "- arxiv_id: 2401.12345\n"
            "- title: Deterministic RAG Evaluation\n"
            "- authors: Example et al.\n"
            "- year/venue: 2024 arXiv\n\n"
            "## evidence\n\nabstract_only\n\n"
            "## results\n\nDeterministic method and result.\n"
        )
    if stage == "research_map":
        return "# Research map\n\nRAG evaluation connects evidence quality and benchmarks.\n"
    if stage == "judge_synthesizer":
        return "# Judge panel\n\noverall_verdict: acceptable\n\nEvidence is sufficient for E2E.\n"
    if stage.endswith("judge"):
        return f"# {stage}\n\nverdict: acceptable\n\nEvidence is sufficient for E2E.\n"
    if stage == "survey_outline":
        return (
            "# Survey Outline\n\n"
            "# Title\n\n**Retrieval-Augmented Generation Survey**\n\n"
            "# Abstract\n\nThis deterministic report verifies the real Survey graph.\n\n"
            "# Ordered section list\n\n## 01 Introduction\n"
        )
    if stage == "section_expander":
        return "## Introduction\n\nThis deterministic section is grounded in [2401.12345].\n"
    return f"# {path}\n\nDeterministic E2E artifact.\n"


def _write(path: str, content: str) -> dict[str, Any]:
    return _tool_call("fs", {"action": "write", "filePath": path, "content": content})


def _handoff(
    path: str | None = None,
    *,
    status: str = "ok",
    preserve_contract_failure: bool = False,
) -> dict[str, Any]:
    lines = ["run_dir: ."]
    if path is not None:
        lines.append(f"artifact: ./{path}")
    lines.append(f"status: {status}")
    if preserve_contract_failure:
        lines.append("risks: OMIT_E2E_CANDIDATE_POOL")
    return {"role": "assistant", "content": "\n".join(lines)}


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/stats")
async def stats() -> dict[str, object]:
    return {"request_counts": dict(sorted(_REQUEST_COUNTS.items()))}


@app.post("/{path:path}", response_model=None)
async def completion(request: Request, path: str) -> Any:
    del path
    body = await request.json()
    messages = body.get("messages", [])
    roles = [message.get("role") for message in messages if isinstance(message, dict)]
    if any(left == right == "system" for left, right in pairwise(roles)):
        return JSONResponse(
            status_code=422,
            content={"error": {"message": "adjacent system messages are not supported"}},
        )
    names = _tool_names(body)
    serialized = json.dumps(body)
    is_title_request = "SCHOLIGHT_SURVEY_NAVIGATION_TITLE" in serialized
    if is_title_request:
        _REQUEST_COUNTS["title"] += 1
        if body.get("thinking") != {"type": "disabled"}:
            raise AssertionError("Survey title generation must disable thinking")
        return _response(
            {"role": "assistant", "content": "Retrieval-Augmented Generation Evaluation"},
            finish_reason="stop",
        )

    called = _called_tools(messages)
    called_names = [name for name, _arguments in called]
    tool_results = [
        message
        for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"
    ]
    stage = _stage(body)
    if stage is None:
        _REQUEST_COUNTS["draft"] += 1
        if "SLOW_E2E_DRAFT" in serialized and not tool_results:
            await asyncio.sleep(15)
        search_name = _tool_with_suffix(names, "search_papers")
        if search_name is not None and search_name not in called_names:
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
        return _response(
            {
                "role": "assistant",
                "content": (
                    "# Research requirement\n\n"
                    "Evaluate retrieval-augmented generation methods, evidence, and benchmarks."
                ),
            },
            finish_reason="stop",
        )

    _REQUEST_COUNTS[stage] += 1
    preserve_contract_failure = "OMIT_E2E_CANDIDATE_POOL" in serialized

    if any(marker in serialized for marker in ("SLOW_E2E_CANCEL", "SLOW_E2E_FORMAL")):
        await asyncio.sleep(30)

    search_name = _tool_with_suffix(names, "search_papers")
    if stage in _SEARCH_STAGES and search_name is not None and search_name not in called_names:
        return _response(
            _tool_call(
                search_name,
                {
                    "query": "retrieval augmented generation evaluation",
                    "strength": "thorough" if stage == "reference_expander" else "standard",
                    "limit": 1,
                },
            ),
            finish_reason="tool_calls",
        )

    if stage == "card_plan":
        written_paths = {
            str(arguments.get("filePath"))
            for name, arguments in called
            if name == "fs" and arguments.get("action") == "write"
        }
        if "00_card_plan.json" not in written_paths:
            return _response(
                _write(
                    "00_card_plan.json",
                    json.dumps(
                        [
                            {
                                "run_dir": ".",
                                "id": "2401.12345",
                                "title": "Deterministic RAG Evaluation",
                                "why": "core_set benchmark evidence",
                            }
                        ]
                    ),
                ),
                finish_reason="tool_calls",
            )
        spawn = _tool_with_suffix(names, "spawn_PaperCard")
        if spawn is not None and spawn not in called_names:
            return _response(
                _tool_call(
                    spawn,
                    {
                        "items": [
                            {
                                "run_dir": ".",
                                "id": "2401.12345",
                                "title": "Deterministic RAG Evaluation",
                                "why": "core_set benchmark evidence",
                            }
                        ],
                        "max_parallel": 1,
                    },
                ),
                finish_reason="tool_calls",
            )
        return _response(
            _handoff(status="ok", preserve_contract_failure=preserve_contract_failure),
            finish_reason="stop",
        )

    if stage == "image_planner":
        return _response(
            _handoff(status="partial", preserve_contract_failure=preserve_contract_failure),
            finish_reason="stop",
        )

    if stage == "survey_outline":
        outline = _ARTIFACTS[stage]
        written_paths = {
            str(arguments.get("filePath"))
            for name, arguments in called
            if name == "fs" and arguments.get("action") == "write"
        }
        if outline not in written_paths:
            return _response(
                _write(outline, _artifact_content(stage, outline)),
                finish_reason="tool_calls",
            )
        if "00_sections.json" not in written_paths:
            return _response(
                _write(
                    "00_sections.json",
                    json.dumps(
                        [
                            {
                                "run_dir": ".",
                                "n": "01",
                                "slug": "introduction",
                                "title": "Introduction",
                                "thesis": "Evaluation evidence determines RAG quality.",
                                "card_ids": ["2401.12345"],
                                "transfer_angle": "",
                            }
                        ]
                    ),
                ),
                finish_reason="tool_calls",
            )
        spawn = _tool_with_suffix(names, "spawn_SectionExpander")
        if spawn is not None and spawn not in called_names:
            return _response(
                _tool_call(
                    spawn,
                    {
                        "items": [
                            {
                                "run_dir": ".",
                                "n": "01",
                                "slug": "introduction",
                                "title": "Introduction",
                                "thesis": "Evaluation evidence determines RAG quality.",
                                "card_ids": ["2401.12345"],
                                "transfer_angle": "",
                            }
                        ],
                        "max_parallel": 1,
                    },
                ),
                finish_reason="tool_calls",
            )
        return _response(
            _handoff(status="ok", preserve_contract_failure=preserve_contract_failure),
            finish_reason="stop",
        )

    artifact = _ARTIFACTS.get(stage)
    if artifact is None:
        raise AssertionError(f"Unhandled real workflow stage: {stage}")
    written_paths = {
        str(arguments.get("filePath"))
        for name, arguments in called
        if name == "fs" and arguments.get("action") == "write"
    }
    if stage == "discovery_merger" and preserve_contract_failure:
        return _response(
            _handoff(artifact, preserve_contract_failure=True),
            finish_reason="stop",
        )
    if artifact not in written_paths:
        return _response(
            _write(artifact, _artifact_content(stage, artifact)),
            finish_reason="tool_calls",
        )
    return _response(
        _handoff(artifact, preserve_contract_failure=preserve_contract_failure),
        finish_reason="stop",
    )
