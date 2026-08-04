"""Public, cacheable product capability discovery."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from scholight.config import get_survey_public_mode

router = APIRouter()


class PublicCapabilitiesResponse(BaseModel):
    """Features that the public client may expose to this deployment."""

    survey: Literal["off", "all"]


@router.get("/capabilities", response_model=PublicCapabilitiesResponse)
async def get_capabilities() -> PublicCapabilitiesResponse:
    """Return fail-closed, non-user-specific product capabilities."""
    return PublicCapabilitiesResponse(survey=get_survey_public_mode())
