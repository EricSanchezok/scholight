"""ArXiv OAI-PMH client — shared by paper_sync and sync_arxiv scripts."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlencode

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

OAI_BASE = "https://oaipmh.arxiv.org/oai"

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


def _oai_url(verb: str, metadata_prefix: str = "arXivRaw", **params: str) -> str:
    extra = urlencode(params) if params else ""
    return (
        f"{OAI_BASE}?verb={verb}&metadataPrefix={metadata_prefix}{('&' + extra) if extra else ''}"
    )


def _oai_resume_url(token: str) -> str:
    return f"{OAI_BASE}?verb=ListRecords&resumptionToken={token}"


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
            url = _oai_resume_url(token)
        else:
            url = _oai_url("ListRecords", **{"from": from_date, "until": until_date})

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
    (created = datestamp from <header>, updated = latest <version><date>).

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

    # Authors: arXivRaw has flat author strings separated by " and "
    def _authors_raw() -> list[str]:
        raw = _tag("authors")
        if not raw:
            return []
        return [a.strip() for a in re.split(r"\s+and\s+", raw) if a.strip()]

    # created = datestamp from <header>
    created = _tag("datestamp", "")
    # updated = most recent <version><date>
    version_dates = re.findall(r"<date>(.*?)</date>", record_xml)
    updated = version_dates[-1] if version_dates else created

    return {
        "arxiv_id": arxiv_id,
        "title": _tag("title"),
        "abstract": _tag("abstract"),
        "authors": _authors_raw(),
        "categories": _tag("categories", "").split(),
        "created": created,
        "updated": updated,
        "license": _tag("license", ""),
        "comments": _tag("comments", ""),
        "doi": _tag("doi", ""),
        "journal_ref": _tag("journal-ref", ""),
        "acm_class": _tag("msc-class", "") or _tag("acm-class", ""),
    }
