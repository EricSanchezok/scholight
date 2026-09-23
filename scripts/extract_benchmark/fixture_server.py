"""Serve the frozen corpus only inside the isolated benchmark network."""

from __future__ import annotations

import json
import os
import time
from collections import deque
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import urlsplit

from corpus import corpus

CASES = {case.name: case for case in corpus()}
REQUESTS: deque[dict] = deque(maxlen=10_000)
REQUEST_LOCK = Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/" + os.environ.get("EXTRACT_BENCH_HTTP_VERSION", "1.0")

    def do_GET(self) -> None:
        name = urlsplit(self.path).path.strip("/")
        if name == "control/requests":
            with REQUEST_LOCK:
                body = json.dumps(list(REQUESTS)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        record = {"path": self.path, "time": time.time(), "peer": list(self.client_address)}
        with REQUEST_LOCK:
            REQUESTS.append(record)
        print(json.dumps({"event": "fixture_request", **record}), flush=True)
        probe_body = None
        if name == "probe/delay":
            time.sleep(0.4)
            name = "article-0"
        elif name == "probe/private":
            time.sleep(0.2)
            probe_body = (
                "Private fixture: " + self.headers.get("X-Fixture-Identity", "missing")
            ).encode()
        elif name == "probe/cookie-start":
            self.send_response(302)
            self.send_header("Set-Cookie", "fixture_session=one; Path=/")
            self.send_header("Location", "/probe/cookie-read?redirect")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif name == "probe/cookie-read":
            probe_body = ("cookie=" + self.headers.get("Cookie", "none")).encode()
        elif name == "probe/retry":
            with REQUEST_LOCK:
                attempts = sum(row["path"] == self.path for row in REQUESTS)
            if attempts == 1:
                self.send_response(503)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            name = "article-0"
        if name.startswith("fault/"):
            parts = name.split("/")
            if parts[1] == "slow":
                time.sleep(10)
                name = "article-0"
            elif parts[1] in {"redirect", "slow-redirect"}:
                count = min(100, int(parts[2]))
                if parts[1] == "slow-redirect":
                    time.sleep(0.3)
                self.send_response(302)
                self.send_header(
                    "Location", f"/fault/{parts[1]}/{count - 1}" if count else "/article-0"
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        if name.startswith("calibration/download/"):
            size = int(name.rsplit("/", 1)[1])
            if size not in {8192, 1_048_576, 8_388_608, 33_554_432, 49_000_000}:
                self.send_error(400)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                for offset in range(0, size, 65_536):
                    self.wfile.write(b"e" * min(65_536, size - offset))
            return
        if probe_body is not None:
            body, mime = probe_body, "text/plain"
        elif name.startswith("calibration/dom/"):
            nodes = int(name.rsplit("/", 1)[1])
            if nodes not in {100, 1000, 5000, 15_000}:
                self.send_error(400)
                return
            body = (
                "<!doctype html><html><body><article>"
                + "<p><a href='#evidence'>Evidence</a> and complete research context.</p>" * nodes
                + "</article><script>document.body.dataset.ready = 'yes';</script></body></html>"
            ).encode()
            mime = "text/html"
        elif name == "manifest":
            body = json.dumps(
                [
                    {
                        "name": c.name,
                        "category": c.category,
                        "expected": c.expected,
                        "status": c.status,
                    }
                    for c in CASES.values()
                ]
            ).encode()
            mime = "application/json"
        elif name in CASES:
            case = CASES[name]
            body, mime = case.body, case.mime
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.end_headers()
        # Expected when a fault-case caller cancels its owned request.
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()  # nosec B104
