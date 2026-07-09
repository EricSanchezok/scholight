"""Named strategies for Level 2 chunk-level search."""

from compass.search.level2.phases import (
    ChunkSearchPhase,
    MaxPAggregationPhase,
    RRFFusionPhase,
)

LEVEL2_STRATEGIES: dict[str, list] = {
    "rrf": [ChunkSearchPhase(), MaxPAggregationPhase(), RRFFusionPhase()],
}
