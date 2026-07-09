"""Scholight API — FastAPI application."""

from scholight import __version__
from scholight.api.app import create_app

__all__ = ["__version__", "create_app"]
