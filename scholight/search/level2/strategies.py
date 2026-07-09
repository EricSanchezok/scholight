"""Named strategies for Level 2 chunk-level search."""

from scholight.search.base import Phase
from scholight.search.level2.phases import (
    ChunkSearchPhase,
    MaxPAggregationPhase,
    RRFFusionPhase,
)

LEVEL2_STRATEGIES: dict[str, list[Phase]] = {
    "rrf": [ChunkSearchPhase(), MaxPAggregationPhase(), RRFFusionPhase()],
}
