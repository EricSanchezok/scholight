"""Serial public acceptance probes using a normally issued, temporary Access Key."""

from __future__ import annotations

import argparse
import json
import os
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from checks import require


def private_read(path: Path) -> dict:
    require(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "Credential file must be private")
    return json.loads(path.read_text())


def private_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = path.with_name(path.name + ".pending-" + uuid4().hex)
    try:
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


class Canary:
    def __init__(self, base: str) -> None:
        require(base.startswith("https://"), "Public canaries require HTTPS")
        self.base = base.rstrip("/")
        self.client = httpx.Client(base_url=self.base, timeout=60, trust_env=False)
        self.previous = 0.0
        self.records: list[dict] = []

    def call(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: dict | None = None,
        expected: int = 200,
        label: str,
        headers: dict | None = None,
    ) -> httpx.Response:
        time.sleep(max(0, self.previous + 10 - time.monotonic()))
        self.previous = time.monotonic()
        before = time.monotonic()
        auth = {"Authorization": "Bearer " + token} if token is not None else {}
        try:
            response = self.client.request(
                method, path, json=body, headers={**auth, **(headers or {})}
            )
        except httpx.RequestError as error:
            self.records.append(
                {
                    "case": label,
                    "time": datetime.now(UTC).isoformat(),
                    "status": 0,
                    "request_id": None,
                    "error_type": type(error).__name__,
                    "duration_ms": (time.monotonic() - before) * 1000,
                }
            )
            raise RuntimeError(f"{label}: transport failed ({type(error).__name__})") from None
        self.records.append(
            {
                "case": label,
                "time": datetime.now(UTC).isoformat(),
                "status": response.status_code,
                "request_id": response.headers.get("X-Request-ID"),
                "duration_ms": (time.monotonic() - before) * 1000,
            }
        )
        require(
            response.status_code == expected,
            f"{label}: HTTP {response.status_code}, expected {expected}",
        )
        return response

    def setup(self, login_file: Path, state_file: Path, name: str) -> None:
        require(not state_file.exists(), "Canary state already exists; reuse or revoke it")
        login = private_read(login_file)
        token = self.call("POST", "/api/auth/login", body=login, label="normal_login").json()[
            "access_token"
        ]
        profile = self.call(
            "GET", "/api/user/profile", token=token, label="account_identity"
        ).json()
        require(
            profile["email"].casefold() == login["email"].casefold(), "Designated account mismatch"
        )
        keys = self.call("GET", "/api/user/access-keys", token=token, label="existing_keys").json()
        require(
            not any(k["name"] == name and k["revoked_at"] is None for k in keys),
            "Matching active canary key requires recovery, not duplicate creation",
        )
        created = self.call(
            "POST",
            "/api/user/access-keys",
            token=token,
            expected=201,
            label="create_canary_key",
            body={"name": name, "expires_at": (datetime.now(UTC) + timedelta(days=12)).isoformat()},
        ).json()
        private_write(
            state_file,
            {
                "base": self.base,
                "name": name,
                "user_id": profile["id"],
                "key_id": created["id"],
                "access_key": created["key"],
                "refresh_cookie": self.client.cookies.get("scholight_refresh"),
            },
        )

    def refresh(self, state: dict, state_file: Path) -> str:
        require(state["base"] == self.base, "Canary host does not match its issued credential")
        self.client.cookies.set(
            "scholight_refresh",
            state["refresh_cookie"],
            domain=urlsplit(self.base).hostname or "",
            path="/api/auth",
        )
        response = self.call("POST", "/api/auth/refresh", label="refresh_canary_session")
        state["refresh_cookie"] = response.cookies.get("scholight_refresh")
        require(bool(state["refresh_cookie"]), "Refresh response did not rotate its cookie")
        private_write(state_file, state)
        return response.json()["access_token"]

    def extract(self, key: str, body: dict, label: str) -> dict:
        return self.call("POST", "/api/extract", token=key, body=body, label=label).json()

    def isolation(self, state: dict, state_file: Path, jwt: str, cursor: str) -> None:
        if state.get("isolation_key_id"):
            self.call(
                "DELETE",
                "/api/user/access-keys/" + state["isolation_key_id"],
                token=jwt,
                expected=204,
                label="recover_isolation_key_cleanup",
            )
        alternate = self.call(
            "POST",
            "/api/user/access-keys",
            token=jwt,
            expected=201,
            label="create_isolation_key",
            body={
                "name": state["name"] + "-isolation",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            },
        ).json()
        state["isolation_key_id"] = alternate["id"]
        private_write(state_file, state)
        try:
            wrong = self.call(
                "POST",
                "/api/extract",
                token=alternate["key"],
                body={"cursor": cursor},
                expected=400,
                label="cross_key_cursor",
            ).json()
            require(wrong["detail"]["code"] == "invalid_cursor", "Cross-key cursor error changed")
        finally:
            self.call(
                "DELETE",
                "/api/user/access-keys/" + alternate["id"],
                token=jwt,
                expected=204,
                label="revoke_isolation_key",
            )
            state.pop("isolation_key_id", None)
            private_write(state_file, state)

    def run(self, state: dict, state_file: Path) -> None:
        jwt = self.refresh(state, state_file)
        profile = self.call("GET", "/api/user/profile", token=jwt, label="login_smoke").json()
        require(profile["id"] == state["user_id"], "Canary session changed identity")
        key = state["access_key"]
        static = self.base + "/extract-canary/static.html"
        full = self.extract(key, {"url": static}, "rest_static")
        for token in [
            "ScholightStaticEvidence2026",
            "ScholightStaticEnd2026",
            "CanaryTable",
            "return value + 42",
            "中文固定证据",
        ]:
            require(token in full["content"], "Static canary lost required content")
        again = self.extract(key, {"url": static}, "rest_cache_reuse")
        require(
            (full["content_hash"], full["fetched_at"])
            == (again["content_hash"], again["fetched_at"]),
            "Cached snapshot changed",
        )
        pdf = self.extract(key, {"url": self.base + "/extract-canary/document.pdf"}, "rest_pdf")
        require("Scholight PDF canary evidence 2026." in pdf["content"], "PDF canary text missing")
        js = self.extract(
            key, {"url": self.base + "/extract-canary/javascript.html"}, "rest_javascript"
        )
        require(
            js["rendered"] and "ScholightJSEvidence2026" in js["content"],
            "JS canary did not render",
        )
        first = self.extract(key, {"url": static, "max_chars": 256}, "pagination_start")
        cursor = first["next_cursor"]
        require(cursor is not None, "Canary must exercise pagination")
        self.isolation(state, state_file, jwt, cursor)
        tampered = self.call(
            "POST",
            "/api/extract",
            token=key,
            body={"cursor": cursor + "x"},
            expected=400,
            label="tampered_cursor",
        ).json()
        require(tampered["detail"]["code"] == "invalid_cursor", "Tampered cursor error changed")
        one = self.extract(key, {"cursor": cursor, "max_chars": 256}, "pagination_read")
        two = self.extract(key, {"cursor": cursor, "max_chars": 256}, "pagination_replay")
        require(one == two, "Cursor replay changed its immutable page")
        content, page = first["content"] + one["content"], one
        while page["next_cursor"] is not None:
            page = self.extract(
                key, {"cursor": page["next_cursor"], "max_chars": 256}, "pagination_continue"
            )
            content += page["content"]
        require(content == full["content"], "Pagination changed complete content")
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        initialized = self.call(
            "POST",
            "/api/mcp",
            token=key,
            label="mcp_initialize",
            headers=headers,
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "scholight-extract-canary", "version": "1"},
                },
            },
        ).json()
        require("result" in initialized, "MCP initialization failed")
        headers["MCP-Protocol-Version"] = initialized["result"]["protocolVersion"]
        self.call(
            "POST",
            "/api/mcp",
            token=key,
            headers=headers,
            expected=202,
            label="mcp_initialized",
            body={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        for index, (fixture, marker) in enumerate(
            [
                ("static.html", "ScholightStaticEvidence2026"),
                ("document.pdf", "Scholight PDF canary evidence 2026."),
                ("javascript.html", "ScholightJSEvidence2026"),
            ],
            2,
        ):
            result = self.call(
                "POST",
                "/api/mcp",
                token=key,
                headers=headers,
                label="mcp_" + fixture,
                body={
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {
                        "name": "extract_url",
                        "arguments": {"url": self.base + "/extract-canary/" + fixture},
                    },
                },
            ).json()["result"]
            require(
                not result.get("isError") and marker in result["structuredContent"]["content"],
                "MCP extraction canary failed",
            )
        search = self.call(
            "POST",
            "/api/search",
            token=key,
            label="search_smoke",
            body={"query": "retrieval augmented generation", "strength": "standard", "limit": 1},
        ).json()
        require(bool(search["hits"]), "Search smoke returned no known-domain evidence")

    def revoke(self, state: dict, state_file: Path) -> None:
        token = self.refresh(state, state_file)
        if state.get("isolation_key_id"):
            self.call(
                "DELETE",
                "/api/user/access-keys/" + state["isolation_key_id"],
                token=token,
                expected=204,
                label="revoke_pending_isolation_key",
            )
        self.call(
            "DELETE",
            "/api/user/access-keys/" + state["key_id"],
            token=token,
            expected=204,
            label="revoke_canary_key",
        )
        self.call("POST", "/api/auth/logout", token=token, label="close_canary_session")
        state_file.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "run", "revoke"])
    parser.add_argument("--base", required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--login-file", type=Path)
    parser.add_argument("--name", default="extract-canary-20260923")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), "Preserve each canary report without overwriting evidence")
    canary = Canary(args.base)
    passed = False
    try:
        if args.action == "setup":
            require(args.login_file is not None, "Setup requires the designated private login file")
            canary.setup(args.login_file, args.state_file, args.name)
        else:
            saved = private_read(args.state_file)
            if args.action == "run":
                canary.run(saved, args.state_file)
            else:
                canary.revoke(saved, args.state_file)
        passed = True
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {"action": args.action, "passed": passed, "records": canary.records}, indent=2
            )
        )
        canary.client.close()
