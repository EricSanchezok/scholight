"""Level 2 pipeline — dual-path chunk search + RRF fusion."""

from __future__ import annotations

from scholight.search.base import Phase, Pipeline


class Level2Pipeline(Pipeline):
    """Dual-path Level 2: independent chunk search → MaxP aggregation → RRF fusion."""

    def __init__(self, extra_phases: list[Phase] | None = None) -> None:
        if extra_phases:
            self.phases = list(extra_phases)
        else:
            from scholight.search.level2.strategies import LEVEL2_STRATEGIES

            self.phases = list(LEVEL2_STRATEGIES["rrf"])
