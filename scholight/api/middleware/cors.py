"""CORS middleware configuration for the FastAPI application."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from scholight.config import settings


def setup_cors(app: FastAPI) -> None:
    """Configure CORS middleware from application settings.

    Refresh tokens use an HttpOnly cookie, while access tokens use the
    Authorization header. Configured origins are explicit, never wildcarded.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
