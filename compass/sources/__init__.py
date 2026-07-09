"""Data source connectors."""

from compass.sources.arxiv import OAIHarvestError, iter_papers_oai

__all__ = ["OAIHarvestError", "iter_papers_oai"]
