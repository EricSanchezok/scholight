"""Export the current FastAPI OpenAPI contract without starting application lifespan."""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("SCHOLIGHT_AUTH_JWT_SECRET", "openapi-export-jwt-secret-value-32b")
os.environ.setdefault(
    "SCHOLIGHT_ANONYMOUS_QUOTA_HMAC_SECRET", "openapi-export-hmac-secret-value-32"
)
os.environ.setdefault("SCHOLIGHT_ACCESS_KEY_HMAC_SECRET", "openapi-export-key-secret-value-32b")
os.environ.setdefault(
    "SCHOLIGHT_MCP_DELEGATION_JWT_SECRET", "openapi-export-delegation-secret-value-32b"
)
os.environ.setdefault("SCHOLIGHT_CORS_ALLOW_ORIGINS", '["http://127.0.0.1:7200"]')
os.environ.setdefault("SCHOLIGHT_ZILLIZ_URI", "https://zilliz.example.invalid")
os.environ.setdefault("SCHOLIGHT_ZILLIZ_TOKEN", "openapi-export-fixture-token")
os.environ.setdefault("SCHOLIGHT_EMBEDDING_BASE_URL", "https://embedding.example.invalid/v1")

from scholight.api.app import create_app


def main() -> None:
    destination = Path(__file__).parents[1] / "src" / "api" / "openapi.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(create_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True)
    destination.write_text(f"{serialized}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
