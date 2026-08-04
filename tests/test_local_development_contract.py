from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_scholight_uses_registered_shared_local_ports() -> None:
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    script = (ROOT / "scripts/dev.sh").read_text(encoding="utf-8")
    vite = (ROOT / "frontend/vite.config.ts").read_text(encoding="utf-8")
    playwright = (ROOT / "frontend/playwright.config.ts").read_text(encoding="utf-8")

    assert "SCHOLIGHT_PG_HOST=127.0.0.1" in environment
    assert "SCHOLIGHT_PG_PORT=55432" in environment
    assert "SCHOLIGHT_PG_USER=scholight_app" in environment
    assert "SCHOLIGHT_PG_SSL_ROOT_CERT=disable" in environment
    assert "SCHOLIGHT_PUBLIC_WEB_URL=http://127.0.0.1:7200" in environment
    assert 'SCHOLIGHT_CORS_ALLOW_ORIGINS=["http://127.0.0.1:7200"]' in environment
    assert "SCHOLIGHT_SERVER_PORT=7201" in environment
    assert "SCHOLIGHT_HOST_PORT=7201" in environment
    assert "SCHOLIGHT_FRONTEND_PORT=7200" in environment
    assert "SCHOLIGHT_EXTRACT_SERVICE_URL=http://127.0.0.1:7202" in environment
    assert "SCHOLIGHT_DISABLE_DOTENV=1" in script
    assert "127.0.0.1" in script
    assert "7200" in vite
    assert "http://127.0.0.1:7201" in vite
    assert "strictPort: true" in vite
    assert "http://127.0.0.1:7200" in playwright


def test_local_catalog_does_not_name_remote_stateful_services() -> None:
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert ".rds.amazonaws.com" not in environment
    assert "SCHOLIGHT_SURVEY_S3_ENDPOINT_URL=http://127.0.0.1:59000" in environment
    assert "SCHOLIGHT_ZILLIZ_TOKEN=" in environment
