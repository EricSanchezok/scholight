"""Domain errors raised by the search core."""

from __future__ import annotations


class SearchInvariantError(Exception):
    """A final search candidate violates a required core invariant."""


class SearchUnavailable(Exception):  # noqa: N818 - domain name is part of the API contract
    """A required Level 1 search dependency failed operationally."""

    def __init__(self, *, phase_name: str, cause: Exception) -> None:
        self.phase_name = phase_name
        self.cause = cause
        super().__init__(f"Search phase {phase_name!r} is unavailable")


class ThoroughSearchUnavailable(SearchUnavailable):
    """A required Strict Thorough phase failed operationally."""


__all__ = ["SearchInvariantError", "SearchUnavailable", "ThoroughSearchUnavailable"]
