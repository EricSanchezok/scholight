"""Cross-field validation for bounded API capacity settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scholight.config import Settings


def test_thorough_capacity_cannot_exceed_total_capacity() -> None:
    with pytest.raises(ValidationError, match="thorough search capacity"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            search_max_in_flight=4,
            search_thorough_max_in_flight=5,
        )


def test_embedding_keepalive_cannot_exceed_connection_limit() -> None:
    with pytest.raises(ValidationError, match="embedding keep-alive"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            embedding_max_connections=4,
            embedding_max_keepalive_connections=5,
        )
