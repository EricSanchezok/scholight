"""Pre-built Level 1 phase strategies for different speed/quality tradeoffs."""

from __future__ import annotations

from scholight.search.base import Phase
from scholight.search.level1.phases import AnnSearchPhase, EmbedPhase, FusionPhase

LEVEL1_STRATEGIES: dict[str, list[Phase]] = {
    "fast": [EmbedPhase(), AnnSearchPhase()],
    "hybrid_fusion": [EmbedPhase(), AnnSearchPhase(), FusionPhase()],
}
