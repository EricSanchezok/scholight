"""Best-effort release of unused native allocator pages in the parser process."""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from functools import cache
from typing import cast


@cache
def _load_trim() -> Callable[[int], int] | None:
    if sys.platform != "linux":
        return None
    try:
        trim = ctypes.CDLL(None).malloc_trim
    except (AttributeError, OSError):
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return cast("Callable[[int], int]", trim)


def release_unused_heap() -> bool:
    """Called after parse locals and handled exception frames have been released.

    An unsupported allocator, a no-op or maintenance failure cannot replace the
    document result. Admission still uses a fresh measured working set.
    """
    try:
        trim = _load_trim()
        return trim is not None and bool(trim(0))
    except Exception:
        return False
