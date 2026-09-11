"""Production credentials are available only to manually selected personal releases."""

import re
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".github/workflows").is_dir())


def test_active_aws_workflows_cannot_publish_on_push_or_use_retired_environments():
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        text = path.read_text()
        if "aws-actions/configure-aws-credentials@" not in text:
            continue
        triggers = text.split("permissions:")[0]
        assert not re.search(r"^  (push|workflow_run|release):", triggers, re.M), path.name
        assert not re.search(
            r"^    environment: (production|image-publish|database-production|"
            r"infrastructure-production)$",
            text,
            re.M,
        ), path.name
        assert "allowed-account-ids:" in text, path.name
        assert "669409472143" in text, path.name
