"""Non-secret immutable identity of a destination and its materialization configuration."""

import hashlib
import json
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from scholight.config import settings


def digest_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class IngestionTarget:
    endpoint: str
    papers_id: str
    chunks_id: str
    embedding_model: str
    embedding_dim: int

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Destination endpoint must not contain credentials or query parameters"
            )
        if not self.papers_id or not self.chunks_id or self.embedding_dim < 1:
            raise ValueError("Destination requires actual collection IDs and a positive dimension")

    @property
    def key(self) -> str:
        return digest_json(asdict(self))


def fulltext_configuration() -> dict[str, object]:
    """Version the retained parser/chunker contract separately from collection identity."""
    return {
        "pipeline": "exact-version-markdown-v1",
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "chunker": "markdown-semantic-v1",
    }


def fulltext_profile_hash() -> str:
    return digest_json(fulltext_configuration())
