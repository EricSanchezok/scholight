"""arXiv metadata connectors and canonical ID parsing."""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from typing import Any
from urllib.parse import urlencode

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

OAI_PRIMARY = "https://oaipmh.arxiv.org/oai"
OAI_FALLBACK = "https://export.arxiv.org/oai2"
API_BASE = "https://export.arxiv.org/api/query"
API_DELAY_SECONDS = 3.0
API_PAGE_SIZE = 2000
API_TOTAL_LIMIT = 30_000
API_ID_BATCH_LIMIT = 500

_RESUMPTION_RE = re.compile(r"<resumptionToken[^>]*>(.*?)</resumptionToken>", re.DOTALL)
_RECORD_RE = re.compile(r"<record>.*?</record>", re.DOTALL)

# ── arXiv ID canonicalization ───────────────────────────────────────────────

#   YYMM.NNNN   (2007-2014, 4-digit suffix, trailing-zero padded)
#   YYMM.NNNNN  (2015+,     5-digit suffix, trailing-zero padded)
#   archive/YYMMNNN (pre-2007)
_OLD_ID_RE = re.compile(r"^[a-z][a-z-]+/\d{7}$")
_DOT_ID_RE = re.compile(r"^(\d{2,4})\.(\d{1,5})$")


def canonicalize_arxiv_id(raw: str) -> str | None:
    """Return a canonical arXiv ID, or ``None`` if irreparable.

    * Valid canonical IDs pass through unchanged.
    * Short IDs are repaired by padding:
      - prefix (YYMM): leading zero to 4 digits
      - suffix:        trailing zero to 4 digits (2007-2014) or 5 digits (2015+)
    * Garbage input returns ``None`` (caller must decide: skip or log).

    Examples::

        canonicalize_arxiv_id("0905.22510") → "0905.2251"   (canonical, pass-through)
        canonicalize_arxiv_id("801.0001")   → "0801.0001"   (prefix padded)
        canonicalize_arxiv_id("1002.49")    → "1002.4900"   (suffix padded)
        canonicalize_arxiv_id("1501.0008")  → "1501.00080"  (2015+, 5-digit suffix)
        canonicalize_arxiv_id("astro-ph/9411001") → "astro-ph/9411001"
        canonicalize_arxiv_id("garbage")    → None
    """
    aid = raw.strip()

    # Old-subject: accept canonical, reject unknown
    if "/" in aid:
        return aid if _OLD_ID_RE.match(aid) else None

    m = _DOT_ID_RE.match(aid)
    if not m:
        return None

    prefix_raw, suffix_raw = m.group(1), m.group(2)
    prefix = prefix_raw.zfill(4)  # "801" → "0801"
    yy = int(prefix[:2])
    target = 5 if yy >= 15 else 4  # 2015+ → 5-digit suffix
    suffix = suffix_raw.ljust(target, "0")  # "49" → "4900" or "0008" → "00080"

    return f"{prefix}.{suffix}"


class OAIHarvestError(Exception):
    """OAI-PMH harvesting failed."""


def _oai_url(
    verb: str,
    metadata_prefix: str = "arXivRaw",
    *,
    base: str = OAI_PRIMARY,
    **params: str,
) -> str:
    extra = urlencode(params) if params else ""
    return f"{base}?verb={verb}&metadataPrefix={metadata_prefix}{('&' + extra) if extra else ''}"


def _oai_resume_url(token: str, *, base: str = OAI_PRIMARY) -> str:
    return f"{base}?verb=ListRecords&resumptionToken={token}"


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=5, max=120),
    retry=retry_if_exception_type((httpx.HTTPError, OAIHarvestError)),
    reraise=True,
)
async def _fetch_oai_page(url: str, logger: Any | None = None) -> str:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30, connect=15), follow_redirects=True
    ) as client:
        resp = await client.get(url)
        # Handle HTTP 503 — arXiv throttle signal with Retry-After
        if resp.status_code == 503:
            retry_after = resp.headers.get("Retry-After", "20")
            try:
                wait_sec = int(retry_after)
            except ValueError:
                wait_sec = 20
            wait_sec = int(wait_sec * 1.5)  # Safety margin
            if logger:
                logger.warning("OAI-PMH 503 throttle", retry_after=retry_after, wait_sec=wait_sec)
            raise OAIHarvestError(f"HTTP 503 — retry after {wait_sec}s")
        resp.raise_for_status()
        body = resp.text
        if "<error" in body:
            code = re.search(r"""<error[^>]*code=['"]([^'"]*)['"]""", body)
            msg = re.search(r"<error[^>]*>([^<]*)</error>", body)
            error_code = code.group(1) if code else "?"
            error_msg = msg.group(1) if msg else "?"
            # "noRecordsMatch" is not a real error — arXiv had no papers that day
            # (e.g. weekends, holidays).  Treat as empty result.
            if error_code == "noRecordsMatch":
                return ""
            raise OAIHarvestError(f"OAI-PMH error {error_code}: {error_msg}")
        return body


async def iter_papers_oai(
    from_date: str,
    until_date: str,
    resume_token: str = "",
    logger: Any | None = None,
    base: str = OAI_PRIMARY,
) -> list[dict[str, Any]]:
    """Fetch paper metadata from OAI-PMH ListRecords (async, returns full list).

    Each paper is a dict: arxiv_id, title, abstract, authors, categories,
    created, updated, license, comments, doi, journal_ref, acm_class.

    Handles resumptionToken for large result sets via iterative (non-recursive)
    page fetching.
    """
    all_papers: list[dict[str, Any]] = []
    token = resume_token

    while True:
        if token:
            url = _oai_resume_url(token, base=base)
        else:
            url = _oai_url(
                "ListRecords",
                base=base,
                **{"from": from_date, "until": until_date},
            )

        body = await _fetch_oai_page(url, logger=logger)

        if not body:
            break

        records = _RECORD_RE.findall(body)
        for rec in records:
            paper = _parse_record(rec)
            if paper:
                all_papers.append(paper)

        rt_match = _RESUMPTION_RE.search(body)
        next_token = rt_match.group(1).strip() if rt_match else ""
        if not next_token or next_token == token:
            break

        token = next_token
        # OAI-PMH polite delay: 10-15s between resumptionToken pages
        await asyncio.sleep(10)

    return all_papers


def _parse_record(record_xml: str) -> dict[str, Any] | None:
    """Parse one OAI-PMH arXivRaw <record> into a paper dict.

    arXivRaw uses flat author strings ("Name1 and Name2") and version dates
    (created = first version date, updated = latest version date). The OAI
    header datestamp describes repository harvesting state, so it is only a
    fallback when the record does not include version history.

    Returns None on parse failure.
    """
    # Extract arxiv_id from <header><identifier>oai:arXiv.org:XXXX.XXXXX</identifier>
    id_match = re.search(r"<identifier>oai:arXiv\.org:(.*?)</identifier>", record_xml)
    if not id_match:
        return None
    arxiv_id = canonicalize_arxiv_id(id_match.group(1).strip())
    if arxiv_id is None:
        return None

    def _tag(name: str, default: str = "") -> str:
        m = re.search(f"<{name}>(.*?)</{name}>", record_xml, re.DOTALL)
        return m.group(1).strip() if m else default

    # arXivRaw currently emits display names as a comma-separated string,
    # sometimes with "and" before the final name.
    def _authors_raw() -> list[str]:
        raw = _tag("authors")
        if not raw:
            return []
        return [
            author.strip()
            for author in re.split(r",\s*(?:and\s+)?|\s+and\s+", raw)
            if author.strip()
        ]

    datestamp = _normalize_date(_tag("datestamp", ""))
    version_dates = [
        normalized
        for value in re.findall(r"<date>(.*?)</date>", record_xml)
        if (normalized := _normalize_date(value))
    ]
    created = version_dates[0] if version_dates else datestamp
    updated = version_dates[-1] if version_dates else created

    return {
        "arxiv_id": arxiv_id,
        "title": _tag("title"),
        "abstract": _tag("abstract"),
        "authors": _authors_raw(),
        "categories": _tag("categories", "").split(),
        "created": created,
        "updated": updated,
        "version": len(version_dates) if version_dates else 1,
        "_version_available": bool(version_dates),
        "updated_history": version_dates,
        "license": _tag("license", ""),
        "comments": _tag("comments", ""),
        "doi": _tag("doi", ""),
        "journal_ref": _tag("journal-ref", ""),
        "acm_class": _tag("msc-class", "") or _tag("acm-class", ""),
        "abstract_embedding": [],
        "has_latex": False,
        "has_pdf": False,
        "has_markdown": False,
        "has_chunks": False,
    }


async def oai_health_check(base: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(f"{base}?verb=Identify")
            return response.status_code == 200
    except httpx.HTTPError:
        return False


def _normalize_date(value: str) -> str:
    try:
        return dt.date.fromisoformat(value[:10]).isoformat()
    except (TypeError, ValueError):
        match = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", value)
        if match is None:
            return value[:10]
        month = dt.datetime.strptime(match.group(2), "%b").month
        return dt.date(int(match.group(3)), month, int(match.group(1))).isoformat()


def _parse_api_entry(entry: str) -> dict[str, Any] | None:
    id_match = re.search(r"<id>https?://arxiv\.org/abs/(.*?)</id>", entry)
    if id_match is None:
        return None
    raw_id = id_match.group(1).strip()
    version_match = re.search(r"v(\d+)$", raw_id)
    arxiv_id = canonicalize_arxiv_id(re.sub(r"v\d+$", "", raw_id))
    if arxiv_id is None:
        return None

    def _tag(name: str, default: str = "") -> str:
        match = re.search(f"<{name}>(.*?)</{name}>", entry, re.DOTALL)
        if match is None:
            match = re.search(f"<arxiv:{name}[^>]*>(.*?)</arxiv:{name}>", entry, re.DOTALL)
        return match.group(1).strip() if match else default

    updated = _normalize_date(_tag("updated"))
    return {
        "arxiv_id": arxiv_id,
        "title": _tag("title"),
        "abstract": _tag("summary"),
        "authors": re.findall(r"<author>.*?<name>(.*?)</name>.*?</author>", entry, re.DOTALL),
        "categories": re.findall(r"""<category[^>]*term=["']([^"']+)["']""", entry),
        "created": _normalize_date(_tag("published")),
        "updated": updated,
        "version": int(version_match.group(1)) if version_match else 1,
        "_version_available": version_match is not None,
        "_metadata_fields": {
            "arxiv_id",
            "title",
            "abstract",
            "authors",
            "categories",
            "created",
            "updated",
            "comments",
            "doi",
            "journal_ref",
            *(["version"] if version_match else []),
        },
        "updated_history": [updated] if updated else [],
        "license": "",
        "comments": _tag("comment"),
        "doi": _tag("doi"),
        "journal_ref": _tag("journal_ref"),
        "acm_class": "",
        "abstract_embedding": [],
        "has_latex": False,
        "has_pdf": False,
        "has_markdown": False,
        "has_chunks": False,
    }


async def fetch_papers_api(date: dt.date) -> list[dict[str, Any]]:
    """Fetch one submission day through the rate-limited Atom API fallback."""
    stamp = date.strftime("%Y%m%d")
    # Pass spaces and let httpx encode them. Literal "+" characters become
    # "%2B", which arXiv interprets as part of the query and may reject.
    query = f"submittedDate:[{stamp}0000 TO {stamp}2359]"
    papers: list[dict[str, Any]] = []
    start = 0
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        while start < API_TOTAL_LIMIT:
            response = await client.get(
                API_BASE,
                params={
                    "search_query": query,
                    "start": start,
                    "max_results": API_PAGE_SIZE,
                    "sortBy": "submittedDate",
                    "sortOrder": "ascending",
                },
            )
            if response.status_code in {429, 503}:
                raise OAIHarvestError(f"arXiv API returned {response.status_code}")
            response.raise_for_status()
            entries = re.findall(r"<entry>(.*?)</entry>", response.text, re.DOTALL)
            if not entries:
                break
            papers.extend(
                paper for entry in entries if (paper := _parse_api_entry(entry)) is not None
            )
            if len(entries) < API_PAGE_SIZE:
                break
            start += API_PAGE_SIZE
            await asyncio.sleep(API_DELAY_SECONDS)
    return papers


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=5, max=120),
    retry=retry_if_exception_type((httpx.HTTPError, OAIHarvestError)),
    reraise=True,
)
async def fetch_papers_by_ids(
    arxiv_ids: list[str],
    *,
    timeout_seconds: float = 30,
) -> list[dict[str, Any]]:
    """Fetch metadata for a bounded list of canonical arXiv IDs."""
    ids = list(dict.fromkeys(arxiv_ids))
    if not ids:
        return []
    if len(ids) > API_ID_BATCH_LIMIT:
        raise ValueError(f"At most {API_ID_BATCH_LIMIT} arXiv IDs may be fetched at once")
    if any(canonicalize_arxiv_id(arxiv_id) != arxiv_id for arxiv_id in ids):
        raise ValueError("arxiv_ids must contain canonical IDs")

    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        response = await client.get(
            API_BASE,
            params={"id_list": ",".join(ids), "max_results": len(ids)},
        )
        if response.status_code in {429, 503}:
            raise OAIHarvestError(f"arXiv API returned {response.status_code}")
        response.raise_for_status()

    entries = re.findall(r"<entry>(.*?)</entry>", response.text, re.DOTALL)
    return [paper for entry in entries if (paper := _parse_api_entry(entry)) is not None]
