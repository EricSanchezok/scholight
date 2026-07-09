#!/usr/bin/env python3
"""arxiv_paper_sync.py — robust daily incremental sync with dual-source fallback.

Design:
  1. Per-day fetch atomicity: each day succeeds or fails independently
  2. OAI-PMH primary channel with automatic fallback to Standard API
  3. Handles 800-1500 papers/weekday, Mon peak ~3000 from weekend backlog
  4. Health probe before each OAI fetch — no spinning on dead endpoints
  5. Standard API respects 3s/req rate limit, max_results=2000, start<30000
  6. Date normalization to YYYY-MM-DD for Zilliz Cloud varchar(16)
  7. Auto-date: scans DB for latest paper date, syncs from there minus lookback

Deployment:
  Trigger via cron/systemd timer: ``scholight scheduler paper-sync``.
  Script scans DB, syncs only new days, exits cleanly.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import sys
from pathlib import Path
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scholight.config import settings  # noqa: E402
from scholight.logging import configure_logging  # noqa: E402
from scholight.pipeline.embedder import Embedder  # noqa: E402
from scholight.storage import storage  # noqa: E402
from scholight.store.client import escape_sql  # noqa: E402
from scholight.store.concurrent import insert_arxiv_papers_concurrent  # noqa: E402

# ── Logging ──────────────────────────────────────────────────────────────

_LOG_FILE = storage.log_path("arxiv_sync", "arxiv_paper_sync.log")
configure_logging(
    log_level=settings.log_level,
    use_json=True,
    file_handler=(str(_LOG_FILE), 50_000_000, 5),
)
logger = structlog.get_logger("arxiv-sync")

from scholight.store.client import get_client  # noqa: E402

# ── Configuration ────────────────────────────────────────────────────────

# OAI-PMH
OAI_PRIMARY = "https://oaipmh.arxiv.org/oai"
OAI_FALLBACK = "https://export.arxiv.org/oai2"
OAI_TIMEOUT = 30  # seconds per page

# Standard API
API_BASE = "https://export.arxiv.org/api/query"
API_DELAY = 3.0  # arXiv ToS: 1 req per 3 seconds, single connection
API_MAX_RESULTS = 2000  # maximum per page
API_TOTAL_CAP = 30000  # hard cap — but daily sync never exceeds this

# Sync windows
SYNC_SAFETY_MARGIN = 1  # arXiv papers appear ~1 day after submission
ZERO_GUARD_DAYS = 5
AUTO_LOOKBACK_DAYS = 7
_WRITE_CONCURRENCY = 8

_MAX_SYNC_RETRIES = 5
_SYNC_RETRY_SECONDS = [5, 10, 20, 40, 80]


# ── Error types ─────────────────────────────────────────────────────────


class OAIUnavailableError(Exception):
    """OAI-PMH is down or timing out — trigger fallback."""


class APIRateLimitError(Exception):
    """Standard API returned 403/503 — back off."""


# ── Date helpers ─────────────────────────────────────────────────────────


def _date_to_str(d: dt.date) -> str:
    return d.isoformat()


def _str_to_date(d: str | None) -> dt.date | None:
    if not d:
        return None
    try:
        return dt.date.fromisoformat(d)
    except (ValueError, TypeError):
        logger.warning("date parse failed, ignoring value", raw_value=d)
        return None


def _latest_paper_date() -> dt.date | None:
    """Return the most recent ``updated`` date across all arxiv_papers.

    Uses cursor iteration since Zilliz Cloud ``query()`` has no ``ORDER BY``.
    """
    client = get_client()
    last_id = ""
    latest: dt.date | None = None
    while True:
        flt = f"arxiv_id > '{escape_sql(last_id)}'" if last_id else "arxiv_id != ''"
        rows = client.query(
            "arxiv_papers",
            filter=flt,
            output_fields=["updated"],
            limit=10000,
        )
        if not rows:
            break
        for r in rows:
            d = _str_to_date(r.get("updated"))
            if d and (latest is None or d > latest):
                latest = d
        last_id = rows[-1]["arxiv_id"]
    return latest


# ── OAI-PMH client ──────────────────────────────────────────────────────


def _oai_url(base: str, from_date: str, until_date: str) -> str:
    """Build initial ListRecords URL with date range."""
    return f"{base}?verb=ListRecords&metadataPrefix=arXivRaw&from={from_date}&until={until_date}"


def _oai_resume_url(base: str, token: str) -> str:
    """Build resume URL — MUST NOT include metadataPrefix per OAI-PMH spec (§4.1)."""
    return f"{base}?verb=ListRecords&resumptionToken={token}"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=10, max=60),
    retry=retry_if_exception_type((httpx.HTTPError, OSError)),
    reraise=True,
)
async def _oai_fetch(url: str) -> str:
    async with httpx.AsyncClient(timeout=OAI_TIMEOUT, follow_redirects=True) as hclient:
        resp = await hclient.get(url)
        if resp.status_code == 503:
            retry_after = resp.headers.get("Retry-After", "20")
            try:
                wait = int(float(retry_after) * 1.5)
            except ValueError:
                wait = 20
            logger.warning("OAI-PMH 503", retry_after=retry_after, wait=wait)
            raise OAIUnavailableError(f"OAI 503 backpressure, retry after {wait}s")
        resp.raise_for_status()
        body = resp.text
        if "<error" in body:
            code = re.search(r"""<error[^>]*code=['"]([^'"]*)['"]""", body)
            ec = code.group(1) if code else "?"
            if ec == "noRecordsMatch":
                return ""
            raise OAIUnavailableError(f"OAI error {ec}")
        return body


async def _oai_health_check(base: str) -> bool:
    """Fast probe: can we reach this OAI-PMH endpoint?"""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as hclient:
            resp = await hclient.get(f"{base}?verb=Identify")
            return resp.status_code == 200
    except Exception:
        return False


async def fetch_papers_oai(
    from_date: str, until_date: str, base: str = OAI_PRIMARY
) -> list[dict[str, Any]]:
    """Fetch one date range from OAI-PMH with resumptionToken support."""
    papers: list[dict[str, Any]] = []
    token = ""

    while True:
        url = _oai_resume_url(base, token) if token else _oai_url(base, from_date, until_date)

        body = await _oai_fetch(url)
        if not body:
            break

        records = re.findall(r"<record>.*?</record>", body, re.DOTALL)
        for rec in records:
            p = _parse_oai_record(rec)
            if p:
                papers.append(p)

        rt = re.search(r"<resumptionToken[^>]*>(.*?)</resumptionToken>", body, re.DOTALL)
        next_token = (rt.group(1) or "").strip() if rt else ""
        if not next_token or next_token == token:
            break
        token = next_token
        await asyncio.sleep(10)  # arXiv polite delay between pages

    return papers


def _parse_oai_record(record_xml: str) -> dict[str, Any] | None:
    """Parse arXivRaw OAI-PMH record → paper dict."""
    id_m = re.search(r"<identifier>oai:arXiv\.org:(.*?)</identifier>", record_xml)
    if not id_m:
        return None
    arxiv_id = id_m.group(1).strip()

    def _tag(n: str, d: str = "") -> str:
        m = re.search(f"<{n}>(.*?)</{n}>", record_xml, re.DOTALL)
        return m.group(1).strip() if m else d

    authors_raw = _tag("authors")
    authors = (
        [a.strip()[:256] for a in re.split(r"\s+and\s+", authors_raw) if a.strip()]
        if authors_raw
        else []
    )

    created = _tag("datestamp")
    version_dates = re.findall(r"<date>(.*?)</date>", record_xml)
    updated = version_dates[-1] if version_dates else created
    # Normalize date to YYYY-MM-DD for Zilliz Cloud varchar(16)
    if created and " " in created:
        created = _normalize_date(created)
    if updated and " " in updated:
        updated = _normalize_date(updated)

    return {
        "arxiv_id": arxiv_id,
        "title": _tag("title")[:2048],
        "abstract": _tag("abstract"),
        "authors": authors,
        "categories": _tag("categories", "").split(),
        "created": created,
        "updated": updated,
        "version": len(version_dates) if version_dates else 1,
        "updated_history": [_normalize_date(d) if " " in d else d for d in version_dates],
        "license": _tag("license", ""),
        "comments": _tag("comments", ""),
        "doi": _tag("doi", ""),
        "journal_ref": _tag("journal-ref", ""),
        "acm_class": _tag("msc-class", "") or _tag("acm-class", ""),
        # Embedding / resource placeholders
        "abstract_embedding": [],
        "abstract_bm25": {},
        "has_latex": False,
        "has_pdf": False,
        "has_markdown": False,
        "has_chunks": False,
    }


# ── Standard API fallback ────────────────────────────────────────────────


_MONTH_MAP = {
    "Jan": "01",
    "Feb": "02",
    "Mar": "03",
    "Apr": "04",
    "May": "05",
    "Jun": "06",
    "Jul": "07",
    "Aug": "08",
    "Sep": "09",
    "Oct": "10",
    "Nov": "11",
    "Dec": "12",
}


def _normalize_date(d: str) -> str:
    """Convert 'Mon, 25 Oct 2010 16:03:12 GMT' → '2010-10-25'."""
    m = re.match(r"\w{3},\s*(\d{1,2})\s+(\w{3})\s+(\d{4})\s+", d)
    if m:
        return f"{m.group(3)}-{_MONTH_MAP.get(m.group(2), '01')}-{m.group(1).zfill(2)}"
    try:
        return dt.date.fromisoformat(d[:10]).isoformat()
    except (ValueError, TypeError):
        return d[:10]  # best-effort, caller should length-validate separately


async def fetch_papers_api(from_date: dt.date, until_date: dt.date) -> list[dict[str, Any]]:
    """Fetch one day from standard arXiv API (rate-limited, 3s/req, retries on 429)."""
    from_str = f"{from_date.strftime('%Y%m%d')}0000"
    until_str = f"{until_date.strftime('%Y%m%d')}2359"
    query = f"submittedDate:[{from_str}+TO+{until_str}]"
    papers: list[dict[str, Any]] = []
    start = 0

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as hclient:
        while start < API_TOTAL_CAP:
            url = (
                f"{API_BASE}?search_query={query}"
                f"&start={start}&max_results={API_MAX_RESULTS}"
                f"&sortBy=submittedDate&sortOrder=ascending"
            )
            resp = await hclient.get(url)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "30")
                try:
                    wait = int(float(retry_after))
                except ValueError:
                    wait = 30
                logger.warning("API 429, backing off", wait=wait)
                await asyncio.sleep(wait)
                continue
            if resp.status_code in (403, 503):
                raise APIRateLimitError(f"API {resp.status_code}")
            if resp.status_code != 200:
                raise OAIUnavailableError(f"API returned {resp.status_code}")

            # Parse Atom XML
            body = resp.text
            entries = re.findall(r"<entry>(.*?)</entry>", body, re.DOTALL)
            if not entries:
                break

            for entry_xml in entries:
                p = _parse_api_entry(entry_xml)
                if p:
                    papers.append(p)

            start += API_MAX_RESULTS
            await asyncio.sleep(API_DELAY)

    return papers


def _parse_api_entry(entry_xml: str) -> dict[str, Any] | None:
    """Parse Atom <entry> → paper dict."""
    from scholight.sources.arxiv import canonicalize_arxiv_id

    id_m = re.search(r"<id>http://arxiv\.org/abs/(.*?)</id>", entry_xml)
    if not id_m:
        return None
    raw_id = id_m.group(1).strip()
    raw_id = re.sub(r"v\d+$", "", raw_id)
    arxiv_id = canonicalize_arxiv_id(raw_id)
    if arxiv_id is None:
        return None

    def _tag(n: str, d: str = "") -> str:
        m = re.search(f"<{n}>(.*?)</{n}>", entry_xml, re.DOTALL)
        if not m:
            # Try arxiv namespace
            m2 = re.search(f"<arxiv:{n}[^>]*>(.*?)</arxiv:{n}>", entry_xml, re.DOTALL)
            return m2.group(1).strip() if m2 else d
        return m.group(1).strip()

    # Authors: <author><name>...</name></author>
    authors: list[str] = []
    for am in re.finditer(r"<author>.*?<name>(.*?)</name>.*?</author>", entry_xml, re.DOTALL):
        authors.append(am.group(1).strip()[:256])

    # Categories: <category term="cs.AI" .../>
    categories: list[str] = []
    for cm in re.finditer(r"""<category[^>]*term=["']([^"']*)["']""", entry_xml):
        categories.append(cm.group(1))

    published = _normalize_date(_tag("published"))
    updated = _normalize_date(_tag("updated"))

    return {
        "arxiv_id": arxiv_id,
        "title": _tag("title")[:2048],
        "abstract": _tag("summary"),
        "authors": authors,
        "categories": categories,
        "created": published,
        "updated": updated,
        "version": 1,  # Standard API doesn't expose version count
        "updated_history": [updated] if updated else [],
        "license": "",
        "comments": _tag("comment"),
        "doi": _tag("doi"),
        "journal_ref": _tag("journal_ref"),
        "acm_class": "",
        "abstract_embedding": [],
        "abstract_bm25": {},
        "has_latex": False,
        "has_pdf": False,
        "has_markdown": False,
        "has_chunks": False,
    }


# ── Embedding & ingestion ───────────────────────────────────────────────


def _truncate_bytes(s: str, max_b: int) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_b:
        return s
    return encoded[:max_b].decode("utf-8", errors="ignore")


async def _embed_and_ingest(papers: list[dict[str, Any]]) -> int:
    """Embed + upsert + mark_pending. Returns count processed.

    BM25 sparse vectors (``abstract_bm25``) are auto-populated by Zilliz Cloud
    on insert/upsert via the BM25 Function — no client-side encoding needed.
    """
    if not papers:
        return 0

    # ── Dense embedding ──
    abstracts = [p.get("abstract", "") or "" for p in papers]
    non_empty = [(i, t) for i, t in enumerate(abstracts) if t.strip()]
    embedder = Embedder()
    async with embedder:
        embeddings = await embedder.embed_many([t for _, t in non_empty])
    for (i, _), vec in zip(non_empty, embeddings):
        papers[i]["abstract_embedding"] = vec

    # Normalize
    for p in papers:
        if not p["abstract_embedding"]:
            p["abstract_embedding"] = [0.0] * settings.embedding_dim
        p.setdefault("categories", [])
        p.setdefault("authors", [])
        p.setdefault("created", p.get("updated", ""))
        p.setdefault("version", 1)
        p.setdefault("updated_history", [])
        p.setdefault("license", "")
        p.setdefault("comments", "")
        p.setdefault("doi", "")
        p.setdefault("journal_ref", "")
        p.setdefault("acm_class", "")
        p.setdefault("has_latex", False)
        p.setdefault("has_pdf", False)
        p.setdefault("has_markdown", False)
        p.setdefault("has_chunks", False)
        # Byte-level truncation for all varchar fields
        for key, max_b in [
            ("title", 2048),
            ("abstract", 16384),
            ("created", 16),
            ("updated", 16),
            ("license", 512),
            ("comments", 8192),
            ("doi", 256),
            ("journal_ref", 2048),
            ("acm_class", 256),
        ]:
            val = p.get(key, "")
            if isinstance(val, str) and val:
                p[key] = _truncate_bytes(val, max_b)
        for a in p.get("authors", []):
            if len(a.encode("utf-8")) > 256:
                idx = p["authors"].index(a)
                p["authors"][idx] = _truncate_bytes(a, 256)
        p["updated_history"] = [_truncate_bytes(d, 16) for d in p.get("updated_history", []) if d]
        # BM25 auto-populated by Zilliz Cloud Function — strip empty dict
        # to avoid overwriting existing sparse vectors on upsert.
        if p.get("abstract_bm25") == {} or not p.get("abstract_bm25"):
            p.pop("abstract_bm25", None)

    # Write
    insert_arxiv_papers_concurrent(papers, concurrency=_WRITE_CONCURRENCY)
    return len(papers)


# ── Day sync worker ─────────────────────────────────────────────────────


async def sync_day(date: dt.date, reference: dt.date | None = None) -> tuple[int, str]:
    """Sync one day.

    Retries transient failures (HTTP errors, timeouts) with exponential backoff
    before giving up.  Returns (0, "oai") with zero-guard semantics for recent
    days that genuinely have no papers.
    """
    from_str = _date_to_str(date)
    until_str = _date_to_str(date)
    ref = reference or dt.date.today()
    days_from_ref = (ref - date).days

    for attempt in range(1, _MAX_SYNC_RETRIES + 1):
        # ── OAI-PMH primary and fallback ──
        for oai_base in (OAI_PRIMARY, OAI_FALLBACK):
            if not await _oai_health_check(oai_base):
                continue
            try:
                papers = await fetch_papers_oai(from_str, until_str, base=oai_base)
                count = await _embed_and_ingest(papers)
                if count == 0 and days_from_ref <= ZERO_GUARD_DAYS:
                    logger.info(
                        "OAI returned 0 for recent day, cross-validating via API",
                        date=from_str,
                        days_from_ref=days_from_ref,
                    )
                    break  # fall through to API cross-validation below
                source_label = "oai_fallback" if oai_base == OAI_FALLBACK else "oai"
                return count, source_label
            except (OAIUnavailableError, httpx.HTTPError, OSError) as exc:
                logger.warning(
                    "OAI-PMH failed",
                    date=from_str,
                    oai_base=oai_base,
                    error=str(exc)[:120],
                    attempt=attempt,
                )

        # ── Standard API fallback ──
        try:
            papers = await fetch_papers_api(date, date)
            count = await _embed_and_ingest(papers)
            return count, "api"
        except (APIRateLimitError, httpx.HTTPError, OSError) as exc:
            logger.warning(
                "API failed",
                date=from_str,
                error=str(exc)[:120],
                attempt=attempt,
            )

        if attempt < _MAX_SYNC_RETRIES:
            delay = _SYNC_RETRY_SECONDS[min(attempt - 1, len(_SYNC_RETRY_SECONDS) - 1)]
            logger.info("retrying day after delay", date=from_str, attempt=attempt, delay=delay)
            await asyncio.sleep(delay)

    # All retries exhausted — defer to next daemon cycle.  --auto mode
    # will pick this day up again on the next invocation.
    logger.error("failed after all retries — deferring", date=from_str)
    return 0, "failed"


# ── Main loop ───────────────────────────────────────────────────────────


async def run_sync() -> dict[str, Any]:
    """Sync from the latest paper date minus lookback to today.

    Auto-detects the start point by scanning arxiv_papers for the most
    recent ``updated`` date.  Falls back to today when the database is empty.
    """
    total_papers = 0
    days_synced = 0
    days_failed = 0
    source_counts: dict[str, int] = {"oai": 0, "oai_fallback": 0, "api": 0, "failed": 0}

    today = dt.date.today()
    safe_today = today - dt.timedelta(days=SYNC_SAFETY_MARGIN)

    latest = _latest_paper_date()
    if latest is None:
        logger.warning("no papers in database, starting from today")
        last = safe_today - dt.timedelta(days=1)
    else:
        auto_start = latest - dt.timedelta(days=AUTO_LOOKBACK_DAYS)
        last = auto_start - dt.timedelta(days=1)
        logger.info(
            "auto-detected start from latest paper date",
            latest_in_db=_date_to_str(latest),
            lookback_days=AUTO_LOOKBACK_DAYS,
            first_sync_date=_date_to_str(last + dt.timedelta(days=1)),
        )

    start_from = last + dt.timedelta(days=1)
    if start_from > safe_today:
        logger.info(
            "already up to date",
            last_synced=_date_to_str(last),
            safe_today=_date_to_str(safe_today),
        )
        return {"papers": 0, "days": 0, "days_failed": 0, "sources": source_counts}

    days_behind = (safe_today - start_from).days + 1
    logger.info(
        "sync starting",
        from_date=_date_to_str(start_from),
        to_date=_date_to_str(safe_today),
        days_behind=days_behind,
    )

    current = start_from
    while current <= safe_today:
        logger.info("syncing day", date=_date_to_str(current))
        count, source = await sync_day(current, reference=safe_today)

        if source == "failed":
            days_failed += 1
            source_counts["failed"] += 1
            logger.error("day failed after all retries — deferring", date=_date_to_str(current))
            current += dt.timedelta(days=1)
            continue

        if count == 0 and (safe_today - current).days < ZERO_GUARD_DAYS:
            logger.warning(
                "zero-guard: recent day returned 0 papers, pausing sync",
                date=_date_to_str(current),
                safe_today=_date_to_str(safe_today),
                guard_days=ZERO_GUARD_DAYS,
            )
            break

        total_papers += count
        days_synced += 1
        source_counts[source] += 1
        logger.info("day done", date=_date_to_str(current), count=count, source=source)
        current += dt.timedelta(days=1)

    logger.info(
        "sync complete",
        papers=total_papers,
        days=days_synced,
        days_failed=days_failed,
        last_date=_date_to_str(current - dt.timedelta(days=1)),
        oai_days=source_counts["oai"],
        oai_fallback_days=source_counts["oai_fallback"],
        api_days=source_counts["api"],
    )
    return {"papers": total_papers, "days": days_synced, "sources": source_counts}
