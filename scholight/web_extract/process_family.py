"""Track and reap worker-owned groups, including Playwright's detached Chromium."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess  # nosec B404
import sys
from contextlib import suppress
from pathlib import Path


def enable_subreaping() -> None:
    """Keep orphaned browser descendants reapable by the Linux supervisor."""
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "Cannot supervise orphaned Extract children")


def _processes() -> list[tuple[int, int, int]]:
    rows: list[tuple[int, int, int]] = []
    if sys.platform == "linux":
        for path in Path("/proc").glob("[0-9]*/stat"):
            with suppress(OSError, ValueError, IndexError):
                fields = path.read_text().rsplit(")", 1)[1].split()
                rows.append((int(path.parent.name), int(fields[1]), int(fields[2])))
    else:
        # Development on macOS only. Linux production does not need procps installed.
        output = subprocess.check_output(  # nosec
            ["/bin/ps", "-A", "-o", "pid=,ppid=,pgid="],
            text=True,
            timeout=1,
        )
        for line in output.splitlines():
            pid, parent, group = map(int, line.split())
            rows.append((pid, parent, group))
    return rows


def process_groups(root: int) -> set[int]:
    rows = _processes()
    descendants = {root}
    groups = {root}
    while True:
        added = {pid for pid, parent, _ in rows if parent in descendants} - descendants
        if not added:
            break
        descendants.update(added)
    groups.update(group for pid, _, group in rows if pid in descendants)
    groups.discard(os.getpgrp())
    return groups


def kill_family(root: int, known_groups: set[int]) -> set[int]:
    groups = known_groups | {root}
    groups.discard(os.getpgrp())
    # Stop the worker/driver before walking the tree so it cannot launch a successor.
    for group in groups:
        with suppress(ProcessLookupError):
            os.killpg(group, signal.SIGSTOP)
    try:
        groups.update(process_groups(root))
    finally:
        for group in groups:
            with suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
    return groups


def reap_groups(groups: set[int]) -> None:
    for group in groups:
        with suppress(ChildProcessError):
            while os.waitpid(-group, os.WNOHANG)[0] > 0:
                pass
