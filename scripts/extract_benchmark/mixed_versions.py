"""Exercise old/new API and Extract source pairs through a real local Unix socket.

Each peer imports its own complete package tree. Only document production is
stubbed: this checks wire compatibility, error mapping and immutable pagination,
not extraction quality, authentication or database migrations.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import subprocess  # nosec B404
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "4a8a74281fad79b071de587a5892c1903b77a1a4"
# Local fixture value; never used against a production endpoint.
TOKEN = "local-mixed-version-fixture"  # nosec B105


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def serve(socket: Path) -> None:
    from datetime import UTC, datetime

    import uvicorn

    from scholight.web_extract.engine import ExtractDocument
    from scholight.web_extract.errors import ExtractError
    from scholight.web_extract.service import create_extract_service

    class FixtureEngine:
        async def extract(self, request):
            if request.url.endswith("/fail"):
                raise ExtractError(
                    code="extraction_failed",
                    message="Fixture error",
                    status_code=422,
                    retryable=False,
                )
            return ExtractDocument(
                requested_url=request.url,
                final_url=request.url,
                status_code=200,
                title="Fixture",
                author=None,
                published_at=None,
                content_type="text/html",
                content="abcdefgh",
                rendered=False,
                extractor="fixture",
                warnings=(),
                content_hash="a" * 64,
                fetched_at=datetime(2026, 9, 23, tzinfo=UTC),
                source_bytes=100,
            )

    uvicorn.run(
        create_extract_service(engine=FixtureEngine(), internal_token=TOKEN),
        uds=str(socket),
        log_level="error",
        access_log=False,
    )


async def client(socket: Path) -> None:
    from types import SimpleNamespace
    from unittest.mock import patch

    import httpx

    from scholight.api.extract_execution import (
        ExtractInvocation,
        PublicExtractError,
        execute_public_extract,
    )
    from scholight.config import settings
    from scholight.models.web_extract import ExtractRequest

    settings.extract_service_url = "http://extract"
    settings.extract_internal_token = TOKEN
    original_client = httpx.AsyncClient
    observed = []

    async def capture(request: httpx.Request) -> None:
        observed.append(
            {"fields": sorted(json.loads(request.content)), "headers": sorted(request.headers)}
        )

    def connected(**kwargs):
        return original_client(
            transport=httpx.AsyncHTTPTransport(uds=str(socket)),
            event_hooks={"request": [capture]},
            **kwargs,
        )

    actor = SimpleNamespace(user=SimpleNamespace(id=1), actor_type="access_key", access_key_id=None)
    invocation = ExtractInvocation(actor=actor, request_id="mixed-version", transport="rest")
    with patch("httpx.AsyncClient", connected):
        first = await execute_public_extract(
            ExtractRequest(url="https://example.com/fixture", max_chars=4), invocation
        )
        second = await execute_public_extract(
            ExtractRequest(cursor=first.next_cursor, max_chars=4), invocation
        )
        require(
            (first.content, second.content, second.next_cursor) == ("abcd", "efgh", None),
            "Immutable pagination changed",
        )
        actor.user.id = 2
        try:
            await execute_public_extract(ExtractRequest(cursor=first.next_cursor), invocation)
        except PublicExtractError as error:
            require(error.code == "invalid_cursor", "Cursor error changed")
        else:
            raise AssertionError("Cross-actor cursor was accepted")
        try:
            await execute_public_extract(ExtractRequest(url="https://example.com/fail"), invocation)
        except PublicExtractError as error:
            require(
                error.status_code == 422 and error.code == "extraction_failed",
                "Document error mapping changed",
            )
        else:
            raise AssertionError("Document failure lost its public mapping")
    require(len(observed) == 2, "Pagination must not issue another internal extraction")
    require(
        all(row["fields"] == ["cookies", "headers", "output", "render", "url"] for row in observed),
        "Internal JSON fields changed",
    )
    print(json.dumps({"passed": True, "wire": observed}))


def run(output: Path, baseline_ref: str) -> None:
    output.mkdir(parents=True, exist_ok=False)
    frozen = output / "baseline"
    frozen.mkdir()
    archive = subprocess.check_output(  # nosec
        ["git", "archive", baseline_ref, "scholight"], cwd=ROOT
    )
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(frozen, filter="data")
    # Keep the socket pathname under the macOS/Linux Unix-socket length limit.
    socket = ROOT / "data/mixed-extract.sock"
    if socket.exists():
        raise RuntimeError("Another mixed-version check owns the Unix socket")
    environment = {**os.environ, "SCHOLIGHT_DISABLE_DOTENV": "1"}
    script = str(Path(__file__).resolve())
    pairs = [("baseline", "candidate", frozen, ROOT), ("candidate", "baseline", ROOT, frozen)]
    results = []
    for api_name, service_name, api_source, service_source in pairs:
        name = f"{api_name}-api-{service_name}-extract"
        with (output / f"{name}-service.log").open("w") as log:
            process = subprocess.Popen(  # nosec
                [
                    sys.executable,
                    script,
                    "--peer",
                    "service",
                    "--source",
                    str(service_source),
                    "--socket",
                    str(socket),
                ],
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("Compatibility service failed to start")
                    if socket.exists():
                        break
                    time.sleep(0.05)
                else:
                    raise TimeoutError("Compatibility service did not start")
                result = subprocess.check_output(  # nosec
                    [
                        sys.executable,
                        script,
                        "--peer",
                        "client",
                        "--source",
                        str(api_source),
                        "--socket",
                        str(socket),
                    ],
                    env=environment,
                    text=True,
                    timeout=15,
                )
                (output / f"{name}-client.log").write_text(result)
                record = json.loads(result.splitlines()[-1])
                require(
                    all(
                        ("x-scholight-budget-ms" in wire["headers"]) == (api_name == "candidate")
                        for wire in record["wire"]
                    ),
                    "Expected optional budget headers were not exercised",
                )
                results.append({"pair": name, **record})
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                socket.unlink(missing_ok=True)
    (output / "result.json").write_text(
        json.dumps({"baseline": baseline_ref, "pairs": results}, indent=2)
    )
    print(json.dumps({"passed": len(results), "output": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline-ref", default=BASELINE)
    parser.add_argument("--peer", choices=["service", "client"])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--socket", type=Path)
    args = parser.parse_args()
    if args.peer:
        sys.path.insert(0, str(args.source))
        if args.peer == "service":
            serve(args.socket)
        else:
            asyncio.run(client(args.socket))
    else:
        run(args.output.resolve(), args.baseline_ref)
