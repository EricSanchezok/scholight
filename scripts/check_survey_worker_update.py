#!/usr/bin/env python3
"""Fail closed when a Survey image replacement could interrupt active work."""

from __future__ import annotations

import argparse

IDLE_CONFIRMATION = "SURVEY WORKERS IDLE"


def select_worker_image(
    *,
    current_image: str,
    desired_image: str,
    runtime_enabled: bool,
    metrics_observed: bool,
    draft_running: float,
    full_running: float,
    idle_confirmation: str,
    require_idle: bool,
) -> str:
    """Return the safe image, deferring only the worker when leases are active."""
    if not runtime_enabled:
        return desired_image
    if require_idle:
        if not metrics_observed:
            raise ValueError("Survey worker resource change requires observed activity metrics")
        if draft_running > 0 or full_running > 0:
            raise ValueError("Survey worker resource change requires idle workers")
        return desired_image
    if not current_image or current_image == desired_image:
        return desired_image
    if draft_running > 0 or full_running > 0:
        return current_image
    if metrics_observed:
        return desired_image
    if idle_confirmation != IDLE_CONFIRMATION:
        raise ValueError(
            "Survey worker image update deferred: activity metrics are unavailable; "
            f"verify the database is idle and enter {IDLE_CONFIRMATION!r}"
        )
    return desired_image


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-image", default="")
    parser.add_argument("--desired-image", required=True)
    parser.add_argument("--runtime-enabled", action="store_true")
    parser.add_argument("--metrics-observed", action="store_true")
    parser.add_argument("--draft-running", type=float, default=0)
    parser.add_argument("--full-running", type=float, default=0)
    parser.add_argument("--idle-confirmation", default="")
    parser.add_argument("--require-idle", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    selected = select_worker_image(
        current_image=args.current_image,
        desired_image=args.desired_image,
        runtime_enabled=args.runtime_enabled,
        metrics_observed=args.metrics_observed,
        draft_running=args.draft_running,
        full_running=args.full_running,
        idle_confirmation=args.idle_confirmation,
        require_idle=args.require_idle,
    )
    print(selected)


if __name__ == "__main__":
    main()
