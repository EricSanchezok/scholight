"""Level 1 search pipeline."""

from __future__ import annotations

from compass.search.base import Pipeline
from compass.search.level1.phases import AnnSearchPhase, EmbedPhase, FusionPhase


class Level1Pipeline(Pipeline):
    """Composed Level 1 paper search pipeline.

    Phases run in order: embed → ANN search → fusion.
    Rocchio removed after Zilliz Cloud migration — BM25 is now a built-in Function.
    Custom phase lists can be injected for strategy-based configuration.
    """

    def __init__(self, phases: list | None = None) -> None:
        if phases is not None:
            self.phases = phases
        else:
            self.phases = [EmbedPhase(), AnnSearchPhase(), FusionPhase()]
