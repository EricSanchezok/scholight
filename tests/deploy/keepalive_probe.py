"""Reuse one downstream client across the proxy's upstream idle interval."""

from __future__ import annotations

import sys
import time
from http.client import HTTPConnection
from urllib.parse import urlsplit


def main() -> int:
    target = urlsplit(sys.argv[1])
    if target.scheme != "http" or target.hostname is None:
        raise ValueError("probe requires an http URL")

    connection = HTTPConnection(target.hostname, target.port or 80, timeout=5)
    path = target.path or "/"
    if target.query:
        path = f"{path}?{target.query}"
    try:
        connection.request("POST", path, body=b"")
        first = connection.getresponse()
        first.read()
        if not 200 <= first.status < 300:
            raise RuntimeError(f"first request returned HTTP {first.status}")
        time.sleep(2)
        connection.request("POST", path, body=b"")
        second = connection.getresponse()
        second.read()
        if not 200 <= second.status < 300:
            raise RuntimeError(f"second request returned HTTP {second.status}")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
