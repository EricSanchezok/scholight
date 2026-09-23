"""Serve the frozen corpus only inside the isolated benchmark network."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from corpus import corpus

CASES = {case.name: case for case in corpus()}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/" + os.environ.get("EXTRACT_BENCH_HTTP_VERSION", "1.0")

    def do_GET(self) -> None:
        name = urlsplit(self.path).path.strip("/")
        if name == "manifest":
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
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()  # nosec B104
