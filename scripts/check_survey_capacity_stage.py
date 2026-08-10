#!/usr/bin/env python3
"""Validate the reviewed Survey capacity ladder before an ECS deployment."""

from __future__ import annotations

import argparse

CAPACITY_STAGES = ((1, 1), (2, 2), (4, 4), (8, 8))


def validate_capacity_transition(
    *,
    current_draft: int,
    current_full: int,
    desired_draft: int,
    desired_full: int,
) -> None:
    """Allow the same/lower stage or exactly one reviewed scale-out step."""
    current = (current_draft, current_full)
    desired = (desired_draft, desired_full)
    if current not in CAPACITY_STAGES:
        raise ValueError(f"current Survey capacity {current!r} is not a reviewed stage")
    if desired not in CAPACITY_STAGES:
        raise ValueError(f"desired Survey capacity {desired!r} is not a reviewed stage")
    current_index = CAPACITY_STAGES.index(current)
    desired_index = CAPACITY_STAGES.index(desired)
    if desired_index > current_index + 1:
        next_stage = CAPACITY_STAGES[current_index + 1]
        raise ValueError(
            f"Survey capacity cannot skip validation stages; deploy {next_stage!r} next"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-draft", type=int, required=True)
    parser.add_argument("--current-full", type=int, required=True)
    parser.add_argument("--desired-draft", type=int, required=True)
    parser.add_argument("--desired-full", type=int, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    validate_capacity_transition(
        current_draft=args.current_draft,
        current_full=args.current_full,
        desired_draft=args.desired_draft,
        desired_full=args.desired_full,
    )


if __name__ == "__main__":
    main()
