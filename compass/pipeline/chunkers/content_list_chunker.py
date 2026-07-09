"""Adaptive chunker — converts MinerU / Marker content_list into evenly-sized chunks.

Algorithm (purely structural):
1. Assemble sections from blocks that carry ``text_level`` (heading indicator).
   Works with both MinerU (flat L1) and Marker (hierarchical L2-L4).
2. Allocate chunk quota per section proportional to its length.
3. For each section, greedily fill paragraphs until a flexible upper-bound is
   reached, then cut at the paragraph boundary.  The upper-bound adapts
   downward when the current chunk is already large.
4. Drop sections with no body text (hallucinated / empty headings).
5. Post-process: force-split oversized single-paragraph chunks, then merge
   adjacent mini-chunks (< 80 chars).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from compass.pipeline.chunkers._utils import TARGET_CHARS, _force_split_text

_PARAGRAPH_SPLIT = re.compile(r"\n\n+")

MERGE_MIN = 500  # merge adjacent chunks below this


@dataclass
class Chunk:
    heading: str
    content: str
    chunk_index: int
    section_index: int
    page_start: int


# ── Public API ──────────────────────────────────────────────────────────


def chunk_content_list(content_list: list[dict[str, Any]]) -> list[Chunk]:
    # ── Phase 1: assemble sections ────────────────────────────────────
    sections: list[_FrozenSection] = []
    cur = _Section(heading="", page=0)
    for item in content_list:
        # Any block tagged with text_level (heading indicator) starts a new section.
        # MinerU: all headings are L1 (flat).  Marker: use hierarchical L1-L4.
        if "text_level" in item:
            if cur and (cur.heading or cur.parts):
                sections.append(cur.freeze())
            cur = _Section(heading=_text(item), page=item.get("page_idx", cur.page))
        else:
            txt = _extract_text(item)
            if txt.strip():
                cur.parts.append(txt)
                if cur.page == 0:
                    cur.page = item.get("page_idx", 0)
    if cur and (cur.heading or cur.parts):
        sections.append(cur.freeze())

    T = sum(s.body_len for s in sections)
    if T == 0:
        return []

    N = max(len(sections), round(T / TARGET_CHARS))
    quotas = [max(1, round(N * s.body_len / T)) for s in sections]
    # Floor: any section > 2×target must get at least 2 chunks
    for i, s in enumerate(sections):
        if s.body_len > TARGET_CHARS * 2 and quotas[i] < 2:
            quotas[i] = 2
    diff = N - sum(quotas)
    for _ in range(abs(diff)):
        idx = max(range(len(quotas)), key=lambda i: quotas[i] * (-1 if diff > 0 else 1))
        quotas[idx] += 1 if diff > 0 else -1
        if quotas[idx] < 1:
            quotas[idx] = 1

    # ── Phase 3: per-section adaptive greedy cutting ───────────────────
    raw: list[tuple[str, str, int]] = []
    for sec, k in zip(sections, quotas):
        if not sec.body:
            # Empty heading with no body — skip (e.g. hallucinated headings,
            # author blocks mis-tagged with text_level, figure-only sections).
            continue
        if sec.paras is None or k <= 1 or len(sec.paras) <= 1:
            raw.append((sec.heading, sec.body, sec.page))
            continue

        ideal = max(1, sec.body_len // k)
        paras = sec.paras
        buf: list[str] = []
        buf_len = 0
        for p in paras:
            plen = len(p)
            flex = max(ideal, int(ideal * (1.5 - 0.5 * buf_len / max(ideal, 1))))
            flex = max(flex, plen)
            if buf and buf_len + plen > flex:
                raw.append((sec.heading, "\n\n".join(buf), sec.page))
                buf = [p]
                buf_len = plen
            else:
                buf.append(p)
                buf_len += plen
        if buf:
            raw.append((sec.heading, "\n\n".join(buf), sec.page))

    # ── Phase 4: force-split oversized single-paragraph chunks ─────────
    i = 0
    while i < len(raw):
        h, b, pg = raw[i]
        if len(b) > TARGET_CHARS * 2:
            parts = _force_split_text(b)
            raw[i : i + 1] = [(h, part, pg) for part in parts]
            i += len(parts)
        else:
            i += 1

    # ── Phase 5: merge adjacent mini-chunks ────────────────────────────
    merged: list[tuple[str, str, int]] = []
    i = 0
    while i < len(raw):
        h, b, pg = raw[i]
        if len(b) >= MERGE_MIN or i + 1 >= len(raw):
            merged.append((h, b, pg))
            i += 1
        else:
            h2, b2, _ = raw[i + 1]
            mh = f"{h} / {h2}" if b else h2
            mb = (b + "\n\n" + b2).strip() if b else b2
            merged.append((mh, mb, pg))
            i += 2

    # ── Phase 6: finalise ──────────────────────────────────────────────
    result: list[Chunk] = []
    for ci, (heading, body, page) in enumerate(merged):
        si = next((i for i, s in enumerate(sections) if s.heading == heading), 0)
        result.append(
            Chunk(heading=heading, content=body, chunk_index=ci, section_index=si, page_start=page)
        )
    return result


# ── Internals ──────────────────────────────────────────────────────────


class _Section:
    __slots__ = ("heading", "page", "parts")
    heading: str
    parts: list[str]
    page: int

    def __init__(self, heading: str = "", page: int = 0) -> None:
        self.heading = heading
        self.parts = []
        self.page = page

    def __bool__(self) -> bool:
        return bool(self.heading or self.parts)

    def freeze(self) -> _FrozenSection:
        body = "\n\n".join(self.parts).strip()
        paras = [p.strip() for p in _PARAGRAPH_SPLIT.split(body) if p.strip()] if body else None
        return _FrozenSection(
            heading=self.heading, body=body, page=self.page, paras=paras, body_len=len(body)
        )

    @property
    def body_len(self) -> int:
        return sum(len(p) for p in self.parts)


@dataclass(frozen=True)
class _FrozenSection:
    heading: str
    body: str
    page: int
    paras: list[str] | None
    body_len: int


def _text(item: dict[str, Any]) -> str:
    return _str_or_join(item.get("text", "")).strip()


def _extract_text(item: dict[str, Any]) -> str:
    t = item.get("type", "")
    if t == "text":
        return _str_or_join(item.get("text", ""))
    if t == "equation":
        v = item.get("latex", "") or item.get("text", "") or ""
        return _str_or_join(v)
    if t == "list" and "list_items" in item:
        items = item["list_items"]
        if isinstance(items, list):
            return "\n".join(str(x) for x in items)
    if t in ("table", "image", "chart", "code", "algorithm", "list"):
        return _str_or_join(item.get("text", ""))
    return ""


def _str_or_join(val: object) -> str:
    if isinstance(val, list):
        return " ".join(str(v) for v in val)
    return str(val) if val else ""
