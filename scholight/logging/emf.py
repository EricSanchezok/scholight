"""Tiny CloudWatch Embedded Metric Format emitter with bounded dimensions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

import structlog

logger = structlog.get_logger("scholight.metrics")
MetricUnit = Literal[
    "Count",
    "Milliseconds",
    "Seconds",
    "Percent",
    "Bytes",
    "None",
]
_ALLOWED_DIMENSIONS = frozenset({"service", "strength", "transport", "outcome"})


def emit_emf(
    *,
    service: str,
    metrics: Mapping[str, tuple[float | int, MetricUnit]],
    strength: str | None = None,
    transport: str | None = None,
    outcome: str | None = None,
) -> None:
    """Emit one EMF object without identifiers, queries, or other high-cardinality fields."""
    dimensions = {
        "service": service,
        **({"strength": strength} if strength is not None else {}),
        **({"transport": transport} if transport is not None else {}),
        **({"outcome": outcome} if outcome is not None else {}),
    }
    if not set(dimensions).issubset(_ALLOWED_DIMENSIONS):
        raise ValueError("unsupported EMF dimension")
    definitions = [{"Name": name, "Unit": unit} for name, (_, unit) in metrics.items()]
    try:
        logger.info(
            "emf_metric",
            **dimensions,
            **{name: value for name, (value, _) in metrics.items()},
            _aws={
                "CloudWatchMetrics": [
                    {
                        "Namespace": "Scholight/Production",
                        "Dimensions": [list(dimensions)],
                        "Metrics": definitions,
                    }
                ]
            },
        )
    except Exception:
        # Metrics are best effort. Recursively logging this failure would risk the
        # same broken handler, so it must never affect search or ingestion work.
        return


__all__ = ["MetricUnit", "emit_emf"]
