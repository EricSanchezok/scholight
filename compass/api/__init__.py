"""Academic Compass API — FastAPI application."""

from compass import __version__
from compass.api.app import create_app

__all__ = ["__version__", "create_app"]
