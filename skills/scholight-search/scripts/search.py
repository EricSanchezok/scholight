#!/usr/bin/env python3
"""Minimal JSON-only CLI for the Scholight public search API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_TIMEOUT_SECONDS = 30.0
_EXIT_CONFIG = 2
_EXIT_AUTH = 3
_EXIT_RATE_LIMIT = 4
_EXIT_SERVICE = 5


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search arXiv papers with Scholight.")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="Run one paper search and print upstream JSON.")
    search.add_argument("query", help="Natural-language paper search query.")
    search.add_argument(
        "--strength",
        choices=("standard", "thorough"),
        default="standard",
        help="Search depth (default: standard).",
    )
    search.add_argument("--limit", type=int, default=5, help="Number of results (default: 5).")
    search.add_argument(
        "--category",
        action="append",
        default=[],
        help="arXiv category filter; repeat for multiple values.",
    )
    search.add_argument(
        "--author",
        action="append",
        default=[],
        help="Author filter; repeat for multiple values.",
    )
    search.add_argument("--date-from", help="Earliest submission date (YYYY-MM-DD).")
    search.add_argument("--date-to", help="Latest submission date (YYYY-MM-DD).")
    return parser


def _payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "query": args.query,
        "strength": args.strength,
        "limit": args.limit,
        "filters": {
            "categories": args.category,
            "authors": args.author,
            "date_from": args.date_from,
            "date_to": args.date_to,
        },
    }


def _http_exit(status: int) -> int:
    if status in {401, 403}:
        return _EXIT_AUTH
    if status == 429:
        return _EXIT_RATE_LIMIT
    if status >= 500:
        return _EXIT_SERVICE
    return _EXIT_CONFIG


def _stderr(message: str) -> None:
    sys.stderr.write(f"{message}\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run the CLI and return a stable process exit code."""
    args = _parser().parse_args(list(argv) if argv is not None else None)
    environment = os.environ if environ is None else environ
    api_url = environment.get("SCHOLIGHT_API_URL", "").strip()
    if not api_url:
        _stderr("Configuration error: SCHOLIGHT_API_URL is required.")
        return _EXIT_CONFIG

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    api_key = environment.get("SCHOLIGHT_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(_payload(args), ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    try:
        request = Request(
            f"{api_url.rstrip('/')}/search",
            data=data,
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
        json.loads(body)
    except HTTPError as exc:
        exc.close()
        _stderr(f"HTTP {exc.code}: Scholight request failed.")
        return _http_exit(exc.code)
    except (TimeoutError, URLError) as exc:
        _stderr(f"Network error: {exc}")
        return _EXIT_SERVICE
    except (UnicodeDecodeError, json.JSONDecodeError):
        _stderr("Network error: Scholight returned invalid JSON.")
        return _EXIT_SERVICE
    except ValueError as exc:
        _stderr(f"Configuration error: {exc}")
        return _EXIT_CONFIG

    sys.stdout.write(body)
    if body and not body.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
