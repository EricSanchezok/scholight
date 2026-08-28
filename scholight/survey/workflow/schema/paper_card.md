# PaperCard Contract

A `PaperCard` is a compact, **full-text-grounded** summary of one paper, read
*with the survey's research anchor in mind*. Cards replace abstract-only notes for
the papers that matter most, and are the evidence the survey is written from.

One file per paper at `run_dir/cards/<artifact_stem>.md`. Modern ids use the id
unchanged. For a legacy id, replace its single slash with `-` only in filenames:
`cs/0012009` is stored as `cards/cs-0012009.md` while its header and citations
remain `cs/0012009`.

## Required sections

- `header`: arXiv id, title, authors (if available), year/venue.
- `problem`: the problem the paper addresses (1–2 sentences).
- `method`: the core mechanism in enough detail to *write about* it — the key
  idea, how it works, what is novel. Grounded in the body, not just the abstract.
- `results`: the concrete empirical claims (datasets, metrics, numbers) **with
  their caveats** (which baseline, which setting, which architecture). Note which
  numbers are from the full text vs only the abstract.
- `anchor_relevance`: why this paper matters to the survey's anchor/topic and
  where it sits in the field's structure.
- `transfer`: the cross-domain insight — *could this method or idea transfer to
  the anchor's direction? how, and what would block it?* This is the payoff of
  reading the full text through the anchor lens. If there is no clear transfer,
  say so in one line.
- `evidence`: declare both `level` and a stable `reason`. `level` is
  `html` | `full_text` | `partial` | `abstract_only` — be honest about
  how much of the body you actually read and parsed. `html` means the arXiv HTML
  rendering was used (best structure, formulas arrive as LaTeX); `full_text`
  means a body extraction was used (PDF text or PDF-to-markdown, possible
  column-order issues); `partial` means only part of the paper was read.
- `key_formulas` (optional): 0–3 of the paper's most important formulas as
  verbatim `$$...$$` LaTeX **copied exactly from the extraction you read**, each
  followed by one plain-language sentence saying what it expresses and where it
  comes from (equation number or section). Omit the section entirely when the
  extraction contains no formulas worth carrying forward. Never retype a formula
  from memory and never invent notation.
- `key_results_table` (optional): at most one compact GFM pipe table (≤8 data
  rows) of the paper's headline numbers — suggested columns: method / setting /
  metric / value / baseline. Copy values exactly from the extraction. Below the
  table add one caveat line stating whether the numbers came from the full body
  or only the abstract and any comparability limits. Omit when no comparable
  numbers exist.

Use this exact shape:

```markdown
## evidence
- level: full_text
- reason: pdf_text_extracted
```

Allowed reasons are `html_text_extracted`, `pdf_text_extracted`,
`pdf_markdown_extracted`, `pdf_text_truncated`, `pdf_markdown_truncated`,
`html_text_truncated`, `scanned_pdf`, `pdf_download_failed`, `pdf_text_empty`,
and `pdf_extraction_failed`.

## Rules

- Ground every claim in the paper. Never fabricate numbers, methods, or results.
- If neither the HTML nor the PDF could be read, set `evidence: abstract_only`
  with the applicable stable reason — do not pretend to have read the body and
  do not expose system dependency details in reader-facing prose.
- Keep it compact (≈ a long abstract plus the transfer note), not a reproduction
  of the paper.
