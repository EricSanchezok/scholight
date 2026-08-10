"""Stage immutable workflow contracts inside an RCM Survey workspace."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

_SCHEMA_REFERENCE = re.compile(r"(?<![A-Za-z0-9_./-])schema/([A-Za-z0-9_.-]+\.md)\b")
_WORKSPACE_DIRECTORIES = ("pdfs", "cards", "sections")


class WorkflowResourceError(RuntimeError):
    """The packaged workflow resources cannot be exposed safely to RCM."""


def _default_workflow_root() -> Path:
    return Path(__file__).parent / "workflow"


def referenced_schema_paths(workflow_root: Path | None = None) -> tuple[Path, ...]:
    """Return the safe schema paths referenced by packaged workflow prompts."""
    try:
        root = (workflow_root or _default_workflow_root()).resolve(strict=True)
    except OSError as exc:
        raise WorkflowResourceError("Survey workflow resources are unavailable") from exc
    prompt_root = root / "prompts"
    if not prompt_root.is_dir() or prompt_root.is_symlink():
        raise WorkflowResourceError("Survey workflow prompts are unavailable")

    references: set[Path] = set()
    try:
        prompts = sorted(prompt_root.glob("*.txt"))
        for prompt in prompts:
            prompt_stat = prompt.lstat()
            if (
                not stat.S_ISREG(prompt_stat.st_mode)
                or prompt.is_symlink()
                or not prompt.resolve(strict=True).is_relative_to(root)
            ):
                raise WorkflowResourceError(f"Survey workflow prompt is unsafe: {prompt.name}")
            source = prompt.read_text(encoding="utf-8")
            references.update(Path("schema") / name for name in _SCHEMA_REFERENCE.findall(source))
    except (OSError, UnicodeError) as exc:
        raise WorkflowResourceError("Survey workflow prompts cannot be read") from exc
    if not references:
        raise WorkflowResourceError("Survey workflow prompts declare no schema contracts")

    for relative_path in references:
        candidate = root / relative_path
        try:
            candidate_stat = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise WorkflowResourceError(
                f"Survey workflow schema is missing: {relative_path.as_posix()}"
            ) from exc
        if (
            not stat.S_ISREG(candidate_stat.st_mode)
            or candidate.is_symlink()
            or not resolved.is_relative_to(root)
        ):
            raise WorkflowResourceError(
                f"Survey workflow schema is unsafe: {relative_path.as_posix()}"
            )
    return tuple(sorted(references, key=Path.as_posix))


def stage_workflow_schema(
    run_root: Path,
    *,
    workflow_root: Path | None = None,
) -> tuple[Path, ...]:
    """Copy the exact referenced schemas into the sandboxed RCM run directory."""
    try:
        root = (workflow_root or _default_workflow_root()).resolve(strict=True)
        resolved_run_root = run_root.resolve(strict=True)
    except OSError as exc:
        raise WorkflowResourceError("Survey workflow resources are unavailable") from exc
    if not resolved_run_root.is_dir() or run_root.is_symlink():
        raise WorkflowResourceError("Survey run workspace is unavailable")

    references = referenced_schema_paths(root)
    destination = resolved_run_root / "schema"
    if destination.is_symlink():
        raise WorkflowResourceError("Survey workspace schema cannot be a symbolic link")
    if destination.exists() and not destination.is_dir():
        raise WorkflowResourceError("Survey workspace schema path is not a directory")

    temporary = Path(tempfile.mkdtemp(prefix=".schema-", dir=resolved_run_root))
    try:
        for relative_path in references:
            source = root / relative_path
            target = temporary / relative_path.name
            shutil.copyfile(source, target)
            target.chmod(0o444)

        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)

        for relative_path in references:
            source = root / relative_path
            target = resolved_run_root / relative_path
            target_stat = target.lstat()
            if (
                not stat.S_ISREG(target_stat.st_mode)
                or target.is_symlink()
                or target.read_bytes() != source.read_bytes()
            ):
                raise WorkflowResourceError(
                    f"Survey workflow schema staging failed: {relative_path.as_posix()}"
                )
    except (OSError, WorkflowResourceError) as exc:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if isinstance(exc, WorkflowResourceError):
            raise
        raise WorkflowResourceError("Survey workflow schema staging failed") from exc
    return references


def prepare_workflow_workspace(run_root: Path) -> tuple[Path, ...]:
    """Create the writable paths that RCM agents are contractually allowed to list.

    Some model tool clients treat listing a missing directory as a terminal tool
    failure.  These directories are part of the workflow contract, so the host
    creates them before RCM starts.  Artifact files remain model-owned because the
    RCM filesystem write operation is create-only and must not encounter host
    placeholders at their contract paths.
    """
    try:
        resolved_run_root = run_root.resolve(strict=True)
    except OSError as exc:
        raise WorkflowResourceError("Survey run workspace is unavailable") from exc
    if not resolved_run_root.is_dir() or run_root.is_symlink():
        raise WorkflowResourceError("Survey run workspace is unavailable")

    prepared: list[Path] = []
    try:
        for name in _WORKSPACE_DIRECTORIES:
            destination = resolved_run_root / name
            if destination.is_symlink():
                raise WorkflowResourceError(f"Survey workspace {name} cannot be a symbolic link")
            if destination.exists() and not destination.is_dir():
                raise WorkflowResourceError(f"Survey workspace {name} path is not a directory")
            destination.mkdir(mode=0o755, exist_ok=True)
            prepared.append(destination)

    except (OSError, WorkflowResourceError) as exc:
        if isinstance(exc, WorkflowResourceError):
            raise
        raise WorkflowResourceError("Survey workflow workspace preparation failed") from exc
    return tuple(prepared)


__all__ = [
    "WorkflowResourceError",
    "prepare_workflow_workspace",
    "referenced_schema_paths",
    "stage_workflow_schema",
]
