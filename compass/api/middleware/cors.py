"""CORS middleware configuration for the FastAPI application."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from compass.config import settings


def setup_cors(app: FastAPI) -> None:
    """Configure CORS middleware from application settings.

    Uses ``allow_credentials=False`` because the API uses Bearer token
    authentication (Authorization header), not cookies.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
