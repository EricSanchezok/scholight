"""Reuse one downstream client across the proxy's upstream idle interval."""

from __future__ import annotations

import sys
import time

import httpx


def main() -> int:
    url = sys.argv[1]
    with httpx.Client(timeout=5) as client:
        first = client.post(url)
        first.raise_for_status()
        time.sleep(2)
        second = client.post(url)
        second.raise_for_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
