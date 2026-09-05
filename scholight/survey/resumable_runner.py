"""Host-owned resumable Survey DAG and bounded shard planning."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess  # nosec B404
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import httpx
import structlog

from scholight.config import settings
from scholight.db.client import DBError
from scholight.db.queries_survey import SurveyJob, update_survey_job_progress
from scholight.db.queries_survey_attempts import (
    SurveyCheckpointPointer,
    commit_survey_job_checkpoint,
    record_compute_attempt_diagnostics,
)
from scholight.sources.arxiv import arxiv_artifact_stem
from scholight.survey.checkpoints import SurveyCheckpoint, SurveyCheckpointStore
from scholight.survey.contracts import SurveyLeaseLostError
from scholight.survey.durable_workflow import (
    ArtifactContract,
    DurableSurveyExecutor,
    DurableUnit,
    SurveyArtifactContractError,
)
from scholight.survey.evidence import SurveyEvidenceAuditError, audit_survey_evidence
from scholight.survey.finalizer import SurveyFinalizationError, finalize_survey
from scholight.survey.process import (
    ProcessControl,
    classify_rcm_error,
    is_transient_rcm_error,
    provider_retry_delay_seconds,
    read_sanitized_tail,
    terminate_process_group,
    write_stdin,
)
from scholight.survey.rcm_diagnostics import (
    attempt_failure_details,
    completion_failure_semantics,
    terminal_completion_failure,
)
from scholight.survey.runtime import survey_environment
from scholight.survey.workflow_resources import (
    prepare_workflow_workspace,
    stage_workflow_schema,
)
from scholight.survey.workflow_runtime import workflow_file

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from scholight.survey.worker import SurveyExecutionResult

_PLAN_MAX_BYTES = 2 * 1024 * 1024
_BIBLIOGRAPHY_MAX_BYTES = 512 * 1024
_SECTION_NUMBER = re.compile(r"^[0-9]{2}$")
_SECTION_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REFERENCE_HEADING = re.compile(rb"(?im)^(?:references|bibliography)\s*$")
_REFERENCE_STATUS = re.compile(r"(?im)^status:\s*(completed|empty|partial|failed)\s*$")
_ARXIV_ID = re.compile(r"(?<![A-Za-z0-9./-])(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+/\d{7})(?!\d)")
_CONTEXT_400 = re.compile(r"context|too (?:large|long)|maximum.*tokens|request.*size", re.I)
_THINKING_400 = re.compile(r"reasoning_content|thinking|tool[_ -]?call|tool history", re.I)
_READ_CHUNK_BYTES = 1024 * 1024
_PDF_MAX_BYTES = 64 * 1024 * 1024
_PDF_TEXT_TAIL_BYTES = 8 * 1024 * 1024


class SurveyStageContractError(Exception):
    """A durable stage plan or shard artifact violated its machine contract."""


def _read_plan(run_root: Path, name: str) -> list[object]:
    path = run_root / name
    try:
        if path.stat().st_size > _PLAN_MAX_BYTES:
            raise SurveyStageContractError(f"{name} exceeds the size limit")
        value = json.loads(path.read_bytes())
    except SurveyStageContractError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SurveyStageContractError(f"{name} is invalid") from exc
    if not isinstance(value, list):
        raise SurveyStageContractError(f"{name} must be a JSON array")
    return value


def _short_text(value: object, *, field: str, maximum: int = 2048) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SurveyStageContractError(f"Survey plan field {field} is invalid")
    return value.strip()


def load_card_plan(run_root: Path) -> tuple[dict[str, Any], ...]:
    """Validate the durable paper-card plan and derive safe artifact stems."""
    rows = _read_plan(run_root, "00_card_plan.json")
    if len(rows) > 100:
        raise SurveyStageContractError("Survey card plan exceeds 100 papers")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict) or raw.get("run_dir") != ".":
            raise SurveyStageContractError("Survey card plan entry is invalid")
        paper_id = _short_text(raw.get("id"), field="id", maximum=64)
        stem = arxiv_artifact_stem(paper_id)
        if stem is None or paper_id in seen:
            raise SurveyStageContractError("Survey card plan arXiv ID is invalid or duplicated")
        seen.add(paper_id)
        result.append(
            {
                "run_dir": ".",
                "id": paper_id,
                "stem": stem,
                "title": _short_text(raw.get("title"), field="title", maximum=512),
                "why": _short_text(raw.get("why"), field="why", maximum=2048),
            }
        )
    return tuple(result)


def load_section_plan(
    run_root: Path,
    *,
    card_ids: set[str],
) -> tuple[dict[str, Any], ...]:
    """Validate exact section filenames and references to planned cards."""
    rows = _read_plan(run_root, "00_sections.json")
    if not rows or len(rows) > 60:
        raise SurveyStageContractError("Survey section plan size is invalid")
    result: list[dict[str, Any]] = []
    seen_numbers: set[str] = set()
    seen_slugs: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict) or raw.get("run_dir") != ".":
            raise SurveyStageContractError("Survey section plan entry is invalid")
        number = _short_text(raw.get("n"), field="n", maximum=2)
        slug = _short_text(raw.get("slug"), field="slug", maximum=80)
        if (
            _SECTION_NUMBER.fullmatch(number) is None
            or _SECTION_SLUG.fullmatch(slug) is None
            or number in seen_numbers
            or slug in seen_slugs
        ):
            raise SurveyStageContractError("Survey section identity is invalid or duplicated")
        raw_card_ids = raw.get("card_ids")
        if not isinstance(raw_card_ids, list) or any(
            not isinstance(paper_id, str) or paper_id not in card_ids for paper_id in raw_card_ids
        ):
            raise SurveyStageContractError("Survey section references an unplanned card")
        if len(raw_card_ids) != len(set(raw_card_ids)):
            raise SurveyStageContractError("Survey section card IDs are duplicated")
        seen_numbers.add(number)
        seen_slugs.add(slug)
        result.append(
            {
                "run_dir": ".",
                "n": number,
                "slug": slug,
                "title": _short_text(raw.get("title"), field="title", maximum=512),
                "thesis": _short_text(raw.get("thesis"), field="thesis", maximum=2048),
                "card_ids": list(raw_card_ids),
                "transfer_angle": (
                    raw["transfer_angle"].strip()
                    if isinstance(raw.get("transfer_angle"), str)
                    and len(raw["transfer_angle"]) <= 2048
                    else ""
                ),
                "artifact": f"sections/{number}_{slug}.md",
            }
        )
    if [item["n"] for item in result] != sorted(item["n"] for item in result):
        raise SurveyStageContractError("Survey sections are not in order")
    return tuple(result)


def bibliography_excerpt(
    document: bytes,
    *,
    maximum_bytes: int = _BIBLIOGRAPHY_MAX_BYTES,
) -> tuple[str, bool]:
    """Return only a bounded References/Bibliography suffix from extracted text."""
    if maximum_bytes < 1024:
        raise ValueError("Bibliography maximum must be at least 1024 bytes")
    matches = tuple(_REFERENCE_HEADING.finditer(document))
    selected = document[matches[-1].start() :] if matches else document[-maximum_bytes:]
    truncated = len(selected) > maximum_bytes or (not matches and len(document) > maximum_bytes)
    selected = selected[:maximum_bytes]
    text = selected.decode("utf-8", errors="replace").strip()
    return text, truncated


def merge_reference_shards(
    run_root: Path,
    seeds: tuple[tuple[str, str], ...],
) -> dict[str, int]:
    """Deterministically merge every seed outcome into the compatibility artifact."""
    parts = ["# Citation expansion", "", "result: completed", ""]
    counts: Counter[str] = Counter()
    for paper_id, stem in seeds:
        path = run_root / "reference_results" / f"{stem}.md"
        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise SurveyStageContractError(f"Reference result is missing: {paper_id}") from exc
        match = _REFERENCE_STATUS.search(content)
        if match is None:
            raise SurveyStageContractError(f"Reference result status is missing: {paper_id}")
        counts[match.group(1)] += 1
        parts.extend((f"## Seed {paper_id}", "", content, ""))
    output = run_root / "03b_citation_expansion.md"
    temporary = output.with_suffix(".md.tmp")
    temporary.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    temporary.replace(output)
    return dict(sorted(counts.items()))


class _StageProcessError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        stderr_tail: str = "",
        diagnostics: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.stderr_tail = stderr_tail
        self.diagnostics = diagnostics or {}


async def _persist_attempt_completion_failure(job: SurveyJob, failure: dict[str, object]) -> None:
    attempt_id = getattr(job, "lease_owner", None)
    if not isinstance(attempt_id, UUID):
        return
    details = attempt_failure_details(failure)
    if not details:
        return
    request_class = details.get("provider_request_class")
    failure_class = f"provider_{request_class}" if isinstance(request_class, str) else "provider"
    try:
        await record_compute_attempt_diagnostics(
            attempt_id=attempt_id,
            failure_class=failure_class,
            failure_details=details,
        )
    except DBError:
        # The query layer already emitted a content-free warning. A diagnostic
        # write failure cannot replace the model failure being handled here.
        return


async def _run_rcm_once(
    *,
    unit: DurableUnit,
    job: SurveyJob,
    run_root: Path,
    control: ProcessControl,
    deadline: datetime,
) -> None:
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise _StageProcessError(
            "survey_timed_out",
            "Survey generation exceeded its execution window.",
        )
    workflow = workflow_file(unit.workflow, mcp_url=settings.survey_mcp_url)
    process = await asyncio.create_subprocess_exec(
        "accelerate",
        "run",
        str(workflow),
        "--stream",
        "--purpose-stdin",
        "--run-dir",
        str(run_root),
        env=survey_environment(
            user_id=job.user_id,
            lifetime_seconds=max(60, round(remaining)),
            include_image=unit.name == "image_planner",
        ),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=run_root,
        start_new_session=True,
    )
    await control.attach(process)
    stdout_task = asyncio.create_task(read_sanitized_tail(process.stdout))
    stderr_task = asyncio.create_task(read_sanitized_tail(process.stderr))
    wait_task = asyncio.create_task(process.wait())
    lost_task = asyncio.create_task(control.lease_lost.wait())
    cancel_task = asyncio.create_task(control.cancel_requested.wait())
    try:
        await write_stdin(process, unit.purpose)
        done, _pending = await asyncio.wait(
            {wait_task, lost_task, cancel_task},
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lost_task in done and control.lease_lost.is_set():
            await terminate_process_group(process)
            raise SurveyLeaseLostError("Survey execution lease is no longer owned")
        if cancel_task in done and control.cancel_requested.is_set():
            await terminate_process_group(process)
            raise _StageProcessError("survey_cancelled", "Survey execution was cancelled.")
        if wait_task not in done:
            await terminate_process_group(process)
            raise _StageProcessError(
                "survey_timed_out",
                "Survey generation exceeded its execution window.",
            )
        return_code = await wait_task
        stdout_tail = await stdout_task
        stderr_tail = await stderr_task
        completion_failure = terminal_completion_failure(stdout_tail)
        if completion_failure is not None:
            code, message = completion_failure_semantics(completion_failure)
            await _persist_attempt_completion_failure(job, completion_failure)
            logger.warning(
                "survey_rcm_completion_failed",
                unit=unit.name,
                **completion_failure,
            )
            raise _StageProcessError(
                code,
                message,
                stderr_tail=stderr_tail,
                diagnostics=completion_failure,
            )
        if return_code != 0:
            code, message = classify_rcm_error(stderr_tail)
            if re.search(r"(?:status|http)\D{0,8}400", stderr_tail, re.I):
                code = "survey_model_request_rejected"
                message = "The Survey model rejected this research unit."
            raise _StageProcessError(code, message, stderr_tail=stderr_tail)
    finally:
        if process.returncode is None:
            await terminate_process_group(process)
        for task in (stdout_task, stderr_task, wait_task, lost_task, cancel_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            stdout_task,
            stderr_task,
            wait_task,
            lost_task,
            cancel_task,
            return_exceptions=True,
        )


async def _run_rcm_with_retries(
    *,
    unit: DurableUnit,
    job: SurveyJob,
    run_root: Path,
    control: ProcessControl,
    deadline: datetime,
) -> None:
    for attempt in range(1, settings.survey_provider_max_attempts + 1):
        try:
            await _run_rcm_once(
                unit=unit,
                job=job,
                run_root=run_root,
                control=control,
                deadline=deadline,
            )
            return
        except _StageProcessError as exc:
            if (
                not is_transient_rcm_error(exc.code)
                or attempt >= settings.survey_provider_max_attempts
            ):
                raise
            delay = provider_retry_delay_seconds(
                attempt,
                base=settings.survey_provider_retry_base_seconds,
                maximum=settings.survey_provider_retry_max_seconds,
            )
            await asyncio.sleep(delay)


def _seed_ids(run_root: Path) -> tuple[tuple[str, str], ...]:
    try:
        content = (run_root / "03a_seed_papers.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SurveyStageContractError("Citation seed artifact is unavailable") from exc
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _ARXIV_ID.finditer(content):
        paper_id = re.sub(r"v\d+$", "", match.group(1))
        stem = arxiv_artifact_stem(paper_id)
        if stem is not None and paper_id not in seen:
            seen.add(paper_id)
            result.append((paper_id, stem))
    if not result or len(result) > 10:
        raise SurveyStageContractError("Citation seed count is outside the 1-10 bound")
    return tuple(result)


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_reference_failure(path: Path, *, paper_id: str, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        f"# Reference seed {paper_id}\nstatus: failed\nseed_id: {paper_id}\n"
        f"reason: {reason[:128]}\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _prepare_reference_input(run_root: Path, *, paper_id: str, stem: str) -> bool:
    """Download one PDF and extract only its bounded bibliography in a child process."""
    input_path = run_root / "reference_inputs" / f"{stem}.json"
    pdf_path = run_root / "pdfs" / f"{stem}.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_pdf = pdf_path.with_name(f".{pdf_path.name}.tmp")
    text_path: Path | None = None
    try:
        with (
            httpx.Client(follow_redirects=True, timeout=httpx.Timeout(120)) as client,
            client.stream(
                "GET",
                f"https://arxiv.org/pdf/{paper_id}",
                headers={"User-Agent": "Scholight-Survey/1.0"},
            ) as response,
        ):
            response.raise_for_status()
            size = 0
            with temporary_pdf.open("xb") as output:
                for chunk in response.iter_bytes(_READ_CHUNK_BYTES):
                    size += len(chunk)
                    if size > _PDF_MAX_BYTES:
                        raise SurveyStageContractError("seed_pdf_too_large")
                    output.write(chunk)
        with temporary_pdf.open("rb") as downloaded:
            signature = downloaded.read(5)
        if temporary_pdf.stat().st_size < 5 or signature != b"%PDF-":
            raise SurveyStageContractError("seed_pdf_invalid")
        temporary_pdf.replace(pdf_path)
        pdftotext = shutil.which("pdftotext")
        if pdftotext is None:
            raise SurveyStageContractError("pdftotext_unavailable")
        descriptor, text_name = tempfile.mkstemp(prefix=f".{stem}.", suffix=".txt", dir=run_root)
        os.close(descriptor)
        text_path = Path(text_name)
        completed = subprocess.run(  # nosec B603
            [pdftotext, "-q", str(pdf_path), str(text_path)],
            check=False,
            capture_output=True,
            timeout=120,
        )
        if completed.returncode != 0:
            raise SurveyStageContractError("pdf_extraction_failed")
        text_size = text_path.stat().st_size
        with text_path.open("rb") as source:
            source.seek(max(0, text_size - _PDF_TEXT_TAIL_BYTES))
            tail = source.read(_PDF_TEXT_TAIL_BYTES)
        bibliography, truncated = bibliography_excerpt(tail)
        _write_json_atomic(
            input_path,
            {
                "schema_version": 1,
                "seed_id": paper_id,
                "status": "completed" if bibliography else "empty",
                "truncated": truncated or text_size > _PDF_TEXT_TAIL_BYTES,
                "bibliography": bibliography,
            },
        )
        return bool(bibliography)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, SurveyStageContractError) else type(exc).__name__
        _write_json_atomic(
            input_path,
            {
                "schema_version": 1,
                "seed_id": paper_id,
                "status": "failed",
                "truncated": False,
                "bibliography": "",
                "reason": reason[:128],
            },
        )
        return False
    finally:
        temporary_pdf.unlink(missing_ok=True)
        if text_path is not None:
            text_path.unlink(missing_ok=True)


def _shrink_reference_input(path: Path) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    bibliography = value.get("bibliography") if isinstance(value, dict) else None
    if not isinstance(bibliography, str):
        return
    data = bibliography.encode("utf-8")
    value["bibliography"] = data[: max(1024, len(data) // 2)].decode("utf-8", errors="ignore")
    value["truncated"] = True
    _write_json_atomic(path, value)


async def _run_reference_seed(
    *,
    unit: DurableUnit,
    job: SurveyJob,
    run_root: Path,
    control: ProcessControl,
    deadline: datetime,
) -> dict[str, object] | None:
    item = cast(dict[str, str], json.loads(unit.purpose))
    paper_id = item["seed_id"]
    stem = item["stem"]
    output_path = run_root / "reference_results" / f"{stem}.md"
    input_path = run_root / "reference_inputs" / f"{stem}.json"
    usable = await asyncio.to_thread(
        _prepare_reference_input,
        run_root,
        paper_id=paper_id,
        stem=stem,
    )
    if not usable:
        _write_reference_failure(output_path, paper_id=paper_id, reason="bibliography_unavailable")
        return None
    failure_diagnostics: dict[str, object] | None = None
    try:
        await _run_rcm_with_retries(
            unit=unit,
            job=job,
            run_root=run_root,
            control=control,
            deadline=deadline,
        )
    except _StageProcessError as exc:
        failure_diagnostics = dict(exc.diagnostics) or None
        request_class = exc.diagnostics.get("request_class")
        if exc.code == "survey_model_request_rejected" and (
            request_class == "request_size" or _CONTEXT_400.search(exc.stderr_tail)
        ):
            _shrink_reference_input(input_path)
            try:
                await _run_rcm_once(
                    unit=unit,
                    job=job,
                    run_root=run_root,
                    control=control,
                    deadline=deadline,
                )
            except _StageProcessError as fallback_error:
                failure_diagnostics = dict(fallback_error.diagnostics) or failure_diagnostics
                _write_reference_failure(
                    output_path, paper_id=paper_id, reason="provider_request_size_rejected"
                )
        elif exc.code == "survey_model_request_rejected" and (
            request_class == "thinking_tool_history" or _THINKING_400.search(exc.stderr_tail)
        ):
            fallback = DurableUnit(
                name=unit.name,
                workflow="reference_seed_non_thinking.rcm",
                purpose=unit.purpose,
                artifacts=unit.artifacts,
            )
            try:
                await _run_rcm_once(
                    unit=fallback,
                    job=job,
                    run_root=run_root,
                    control=control,
                    deadline=deadline,
                )
            except _StageProcessError as fallback_error:
                failure_diagnostics = dict(fallback_error.diagnostics) or failure_diagnostics
                _write_reference_failure(
                    output_path, paper_id=paper_id, reason="provider_tool_history_rejected"
                )
        else:
            _write_reference_failure(
                output_path,
                paper_id=paper_id,
                reason=exc.code,
            )
    if not output_path.is_file():
        _write_reference_failure(output_path, paper_id=paper_id, reason="result_missing")
    return failure_diagnostics


def _shard_result_path(run_root: Path, *, kind: str, name: str) -> Path:
    return run_root / "shard_results" / kind / f"{name}.json"


async def _run_nonblocking_shard(
    *,
    unit: DurableUnit,
    job: SurveyJob,
    run_root: Path,
    control: ProcessControl,
    deadline: datetime,
    kind: str,
    stem: str,
    expected_artifact: str,
) -> dict[str, object] | None:
    result_path = _shard_result_path(run_root, kind=kind, name=stem)
    failure_diagnostics: dict[str, object] | None = None
    try:
        await _run_rcm_with_retries(
            unit=unit,
            job=job,
            run_root=run_root,
            control=control,
            deadline=deadline,
        )
        ArtifactContract(expected_artifact).validate(run_root)
        result = {"schema_version": 1, "status": "completed", "unit": unit.name}
    except (_StageProcessError, SurveyArtifactContractError) as exc:
        if isinstance(exc, _StageProcessError):
            failure_diagnostics = dict(exc.diagnostics) or None
        result = {
            "schema_version": 1,
            "status": "failed",
            "unit": unit.name,
            "reason": exc.code if isinstance(exc, _StageProcessError) else "artifact_invalid",
        }
    _write_json_atomic(result_path, result)
    return failure_diagnostics


def _reference_merge_unit(run_root: Path, seeds: tuple[tuple[str, str], ...]) -> None:
    merge_reference_shards(run_root, seeds)


def _progress_component(unit_name: str) -> str:
    if unit_name.startswith("reference_seed:"):
        return "reference_expander"
    if unit_name.startswith("paper_card:"):
        return "paper_card"
    if unit_name.startswith("section:"):
        return "section_expander"
    return {
        "citation_seed": "citation_seed_selector",
        "semantic_expansion": "semantic_expander",
        "cross_domain_expansion": "cross_domain_expander",
        "expansion_merge": "expansion_merger",
        "outline_plan": "survey_outline",
        "final_markdown": "survey_finalizer",
    }.get(unit_name, unit_name)


async def execute_resumable_survey(
    job: SurveyJob,
    run_root: Path,
    *,
    control: ProcessControl,
    checkpoint_store: SurveyCheckpointStore,
    checkpoint_pointer: SurveyCheckpointPointer,
    restored_checkpoint: SurveyCheckpoint | None,
    attempt_id: UUID,
) -> SurveyExecutionResult:
    """Execute the host-owned stage DAG, checkpointing every stage or shard."""
    from scholight.survey.progress import stage_for_component
    from scholight.survey.worker import SurveyExecutionResult

    started_at = datetime.now(UTC)
    timings: list[dict[str, object]] = []
    provider_failures: list[dict[str, object]] = []
    current_sequence = checkpoint_pointer.sequence
    current_hash = checkpoint_pointer.manifest_sha256
    completed = restored_checkpoint.completed_units if restored_checkpoint is not None else ()
    try:
        prepare_workflow_workspace(run_root)
        stage_workflow_schema(run_root)

        async def _checkpoint(unit_name: str, completed_units: tuple[str, ...]) -> None:
            nonlocal current_hash, current_sequence
            checkpoint = await checkpoint_store.publish(
                user_id=job.user_id,
                job_id=job.id,
                run_root=run_root,
                sequence=current_sequence + 1,
                stage=unit_name,
                completed_units=completed_units,
                workflow_version=checkpoint_pointer.workflow_version,
                executor_version=checkpoint_pointer.executor_version,
                parent_manifest_sha256=current_hash,
            )
            committed = await commit_survey_job_checkpoint(
                job_id=job.id,
                attempt_id=attempt_id,
                expected_sequence=current_sequence,
                stage=unit_name,
                manifest_key=checkpoint.manifest_key,
                manifest_sha256=checkpoint.manifest_sha256,
            )
            if not committed:
                raise SurveyLeaseLostError("Survey checkpoint lease is no longer owned")
            current_sequence = checkpoint.sequence
            current_hash = checkpoint.manifest_sha256
            progress = stage_for_component(_progress_component(unit_name))
            if progress is not None:
                await update_survey_job_progress(
                    job_id=job.id,
                    worker_id=attempt_id,
                    stage=progress,
                )

        seed_cache: tuple[tuple[str, str], ...] = ()

        async def _run_unit(unit: DurableUnit) -> None:
            nonlocal seed_cache
            unit_started = time.perf_counter()
            failure_diagnostics: dict[str, object] | None = None
            try:
                if unit.name.startswith("reference_seed:"):
                    failure_diagnostics = await _run_reference_seed(
                        unit=unit,
                        job=job,
                        run_root=run_root,
                        control=control,
                        deadline=checkpoint_pointer.execution_deadline_at,
                    )
                elif unit.name.startswith("paper_card:"):
                    stem = unit.name.split(":", 1)[1]
                    failure_diagnostics = await _run_nonblocking_shard(
                        unit=unit,
                        job=job,
                        run_root=run_root,
                        control=control,
                        deadline=checkpoint_pointer.execution_deadline_at,
                        kind="cards",
                        stem=stem,
                        expected_artifact=f"cards/{stem}.md",
                    )
                elif unit.name == "reference_merge":
                    await asyncio.to_thread(_reference_merge_unit, run_root, seed_cache)
                elif unit.name == "image_planner":
                    failure_diagnostics = await _run_nonblocking_shard(
                        unit=unit,
                        job=job,
                        run_root=run_root,
                        control=control,
                        deadline=checkpoint_pointer.execution_deadline_at,
                        kind="optional",
                        stem="image_planner",
                        expected_artifact="08_global_picture.png",
                    )
                elif unit.name == "final_markdown":
                    await asyncio.to_thread(audit_survey_evidence, run_root)
                    await asyncio.to_thread(finalize_survey, run_root)
                else:
                    await _run_rcm_with_retries(
                        unit=unit,
                        job=job,
                        run_root=run_root,
                        control=control,
                        deadline=checkpoint_pointer.execution_deadline_at,
                    )
            except _StageProcessError as exc:
                if exc.diagnostics:
                    provider_failures.append({"unit": unit.name, **exc.diagnostics})
                raise
            if failure_diagnostics is not None:
                provider_failures.append({"unit": unit.name, **failure_diagnostics})
            timings.append(
                {
                    "component": unit.name,
                    "duration_ms": max(0, round((time.perf_counter() - unit_started) * 1000)),
                }
            )

        executor = DurableSurveyExecutor(
            run_root=run_root,
            completed_units=completed,
            run_unit=_run_unit,
            checkpoint=_checkpoint,
        )
        topic = job.approved_draft
        handoff = json.dumps({"run_dir": "."}, separators=(",", ":"))
        sequential = (
            DurableUnit("anchor", "anchor.rcm", topic, (ArtifactContract("00_survey_spec.md"),)),
            DurableUnit(
                "query_plan",
                "query_plan.rcm",
                handoff,
                (ArtifactContract("01_query_plan.md"),),
            ),
            DurableUnit(
                "discovery",
                "discovery.rcm",
                handoff,
                (ArtifactContract("02_candidate_pool.md"),),
            ),
            DurableUnit(
                "citation_seed",
                "citation_seed.rcm",
                handoff,
                (ArtifactContract("03a_seed_papers.md"),),
            ),
        )
        for unit in sequential:
            await executor.execute(unit)
        seed_cache = _seed_ids(run_root)
        reference_units = tuple(
            DurableUnit(
                f"reference_seed:{stem}",
                "reference_seed.rcm",
                json.dumps(
                    {
                        "run_dir": ".",
                        "seed_id": paper_id,
                        "stem": stem,
                        "input_path": f"reference_inputs/{stem}.json",
                        "output_path": f"reference_results/{stem}.md",
                    },
                    separators=(",", ":"),
                ),
                (ArtifactContract(f"reference_results/{stem}.md"),),
            )
            for paper_id, stem in seed_cache
        )
        await executor.execute_many(reference_units, concurrency=2)
        await executor.execute(
            DurableUnit(
                "semantic_expansion",
                "semantic_expansion.rcm",
                handoff,
                (ArtifactContract("03c_semantic_expansion.md"),),
            )
        )
        await executor.execute(
            DurableUnit(
                "cross_domain_expansion",
                "cross_domain_expansion.rcm",
                handoff,
                (ArtifactContract("03d_cross_domain.md"),),
            )
        )
        await executor.execute(
            DurableUnit(
                "reference_merge",
                "builtin:reference_merge",
                handoff,
                (ArtifactContract("03b_citation_expansion.md"),),
            )
        )
        for unit in (
            DurableUnit(
                "expansion_merge",
                "expansion_merge.rcm",
                handoff,
                (ArtifactContract("03_expansion.md"),),
            ),
            DurableUnit(
                "rank_pool",
                "rank_pool.rcm",
                handoff,
                (ArtifactContract("04_ranked_pool.md"),),
            ),
            DurableUnit(
                "card_plan",
                "card_plan.rcm",
                handoff,
                (ArtifactContract("00_card_plan.json", kind="json"),),
            ),
        ):
            await executor.execute(unit)
        cards = load_card_plan(run_root)
        card_units = tuple(
            DurableUnit(
                f"paper_card:{item['stem']}",
                "paper_card.rcm",
                json.dumps(
                    {key: value for key, value in item.items() if key != "stem"},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                (
                    ArtifactContract(
                        f"shard_results/cards/{item['stem']}.json",
                        kind="json",
                    ),
                ),
            )
            for item in cards
        )
        await executor.execute_many(card_units, concurrency=4)
        for unit in (
            DurableUnit(
                "research_map",
                "research_map.rcm",
                handoff,
                (ArtifactContract("05_research_map.md"),),
            ),
            DurableUnit(
                "judge_panel",
                "judge_panel.rcm",
                handoff,
                (ArtifactContract("06_judge_panel.md"),),
            ),
            DurableUnit(
                "image_planner",
                "image_planner.rcm",
                handoff,
                (ArtifactContract("shard_results/optional/image_planner.json", kind="json"),),
            ),
            DurableUnit(
                "outline_plan",
                "survey_outline.rcm",
                handoff,
                (
                    ArtifactContract("00_outline.json", kind="json"),
                    ArtifactContract("00_outline.md"),
                    ArtifactContract("00_sections.json", kind="json"),
                ),
            ),
        ):
            await executor.execute(unit)
        sections = load_section_plan(run_root, card_ids={str(item["id"]) for item in cards})
        section_units = tuple(
            DurableUnit(
                f"section:{item['n']}",
                "section_expander.rcm",
                json.dumps(
                    {key: value for key, value in item.items() if key != "artifact"},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                (ArtifactContract(str(item["artifact"])),),
            )
            for item in sections
        )
        try:
            await executor.execute_many(section_units, concurrency=4)
        except (_StageProcessError, SurveyArtifactContractError):
            missing = tuple(
                unit for unit in section_units if unit.name not in executor.completed_units
            )
            if not missing:
                raise
            await executor.execute_many(missing, concurrency=4)
        await executor.execute(
            DurableUnit(
                "final_markdown",
                "builtin:finalizer",
                handoff,
                (ArtifactContract("08_survey.md"),),
            )
        )
        evidence = audit_survey_evidence(run_root)
        shard_failures = 0
        for result_path in (run_root / "shard_results").rglob("*.json"):
            try:
                value = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                shard_failures += 1
                continue
            if not isinstance(value, dict) or value.get("status") != "completed":
                shard_failures += 1
        reference_counts = merge_reference_shards(run_root, seed_cache)
        degraded = (
            evidence.coverage_percent < 80
            or shard_failures > 0
            or reference_counts.get("failed", 0) > 0
        )
        diagnostics = {
            "schema_version": 1,
            "executor": checkpoint_pointer.executor_version,
            "checkpoint_sequence": current_sequence,
            "completed_unit_count": len(executor.completed_units),
            "shard_failure_count": shard_failures,
            "reference_status_counts": reference_counts,
            "evidence_coverage_percent": evidence.coverage_percent,
            "provider_failures": provider_failures,
        }
        return SurveyExecutionResult(
            outcome="succeeded",
            error_code="survey_quality_degraded" if degraded else None,
            error_message=(
                "This report was delivered with incomplete quality checks and was not counted "
                "against your Survey allowance."
                if degraded
                else None
            ),
            started_at=started_at,
            finished_at=datetime.now(UTC),
            stage_timings=tuple(timings),
            return_code=0,
            termination_reason="completed_degraded" if degraded else "completed",
            diagnostics=diagnostics,
            chargeable=not degraded,
        )
    except SurveyLeaseLostError:
        raise
    except _StageProcessError as exc:
        if exc.code == "survey_cancelled":
            return SurveyExecutionResult(
                outcome="cancelled",
                error_code=None,
                error_message=None,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                stage_timings=tuple(timings),
                return_code=None,
                termination_reason="cancelled",
            )
        return SurveyExecutionResult(
            outcome="failed",
            error_code=exc.code,
            error_message=exc.public_message,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            stage_timings=tuple(timings),
            return_code=1,
            termination_reason="unit_failed",
            stderr_tail=None,
            diagnostics={"provider_failures": provider_failures},
        )
    except SurveyEvidenceAuditError as exc:
        return SurveyExecutionResult(
            outcome="failed",
            error_code=exc.code,
            error_message=str(exc),
            started_at=started_at,
            finished_at=datetime.now(UTC),
            stage_timings=tuple(timings),
            return_code=1,
            termination_reason="evidence_audit_failed",
        )
    except SurveyFinalizationError as exc:
        return SurveyExecutionResult(
            outcome="failed",
            error_code=exc.code,
            error_message="Survey research finished, but the final report could not be assembled.",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            stage_timings=tuple(timings),
            return_code=1,
            termination_reason="finalization_failed",
        )
    except (SurveyStageContractError, SurveyArtifactContractError) as exc:
        return SurveyExecutionResult(
            outcome="failed",
            error_code="survey_stage_contract_invalid",
            error_message="A required Survey research unit did not produce a valid artifact.",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            stage_timings=tuple(timings),
            return_code=1,
            termination_reason="artifact_contract_failed",
            diagnostics={"error_type": type(exc).__name__},
        )


__all__ = [
    "SurveyStageContractError",
    "bibliography_excerpt",
    "execute_resumable_survey",
    "load_card_plan",
    "load_section_plan",
    "merge_reference_shards",
]
