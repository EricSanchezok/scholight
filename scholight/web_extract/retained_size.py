"""Conservative Python object accounting for bounded process-local caches."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import fields, is_dataclass

from pydantic import BaseModel

# OrderedDict links, hash-table slack and per-entry accounting/index integers.
INDEX_BYTES = 256


def retained_size(value: object) -> int:
    """Count the retained graph, including wide strings, containers and metadata.

    Aliases within one entry are counted once; aliases across entries are counted
    independently, intentionally overestimating shared immutable objects.
    """
    seen: set[int] = set()

    def visit(item: object) -> int:
        if id(item) in seen:
            return 0
        seen.add(id(item))
        size = sys.getsizeof(item)
        if isinstance(item, Mapping):
            return size + sum(visit(key) + visit(value) for key, value in item.items())
        if isinstance(item, (tuple, list, set, frozenset)):
            return size + sum(visit(child) for child in item)
        if isinstance(item, BaseModel):
            return size + visit(vars(item)) + visit(item.model_fields_set) + visit(item.model_extra)
        if is_dataclass(item) and not isinstance(item, type):
            return size + sum(visit(getattr(item, field.name)) for field in fields(item))
        return size

    return visit(value)
