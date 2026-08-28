# Section Contract

The survey is built **section by section**: an outline node emits a JSON list of
section specs, parallel workers expand each section into its own file, and the
application finalizer orders those files into the final survey. This keeps each
writing step's context small while letting every section go deep.

## Section spec (outline → expansion)

Before dispatching workers, the outline writes a **JSON array to
`run_dir/00_sections.json`**, one element per section in final order. The spawn
call uses the same entries; the file preserves expectations across retries and
recovery. Each element:

- `run_dir`: the run directory, repeated verbatim in every element.
- `n`: two-digit order string, e.g. `"01"` — controls file name and final order.
- `slug`: short kebab id, e.g. `"feedback-alignment"`.
- `title`: the section heading.
- `thesis`: one-line claim the section argues (its part of the through-line).
- `card_ids`: exact canonical arXiv `id` values copied from
  `run_dir/00_card_plan.json` for the cards the section draws on. Preserve legacy
  slash ids such as `math/0208020`; do not substitute artifact stems such as
  `math-0208020`.
- `transfer_angle`: a one-line cross-domain / anchor-transfer idea to weave in
  (may be empty).

A typical list: introduction, an evolution / research-arc section, one section
per method family, a synthesizing comparison, open problems, conclusion.

## Section file (expansion → application finalizer)

- Path: `run_dir/sections/<n>_<slug>.md`, starting with `## <title>`.
- Detailed, flowing prose grounded in the cards. Every cited paper must include
  its exact `[arxiv_id]`; an adjacent `(Author, year)` is optional. The application
  finalizer uses those identifiers to build one auditable reference list.
- Formulas: you may quote a cited card's `key_formulas` LaTeX verbatim — inline
  sparingly as `$...$`, and for a pivotal formula as a fenced display block where
  the opening `$$` and closing `$$` each sit on their own line. Never invent,
  retype, or re-notation a formula that is not in a cited card. Write dollar
  amounts as `\$5` so they are not read as math delimiters.
- Tables: when comparing methods across cards, prefer one synthesized GFM pipe
  table built from the cited cards' `key_results_table` entries over paragraphs
  of numbers. Every value must match the card exactly; keep the cards' caveats
  in the surrounding prose.
- Charts: when **at least two cited cards report comparable numbers on a shared
  metric or setting**, you may declare one `chart` fenced code block (JSON) and
  the application renders it as a local figure at finalization. The block is
  replaced by a `figures/` image link, so never write the image link yourself.
  - `type`: `line` | `bar` | `grouped_bar` | `scatter` | `pie` | `flow`.
  - Shared fields: `type`, optional `title`, `caption` (≤200 chars, states the
    data source and comparability limit).
  - `line`/`scatter`: `x` (numbers) and `series` (`[{name, y}]`), all `y` the
    same length as `x`. `bar`: `x` (category strings), one `series`, optional
    `orientation` (`vertical` default | `horizontal`). `grouped_bar`: as `bar`
    with 2–8 series. `pie`: `labels` (strings) plus one `series` `y` with
    non-negative values. `flow`: `direction` (`TB` default | `LR`), `nodes`
    (`[{id, label}]`), `edges` (`[{from, to, label?}]` referencing existing
    node ids) — use it for a method pipeline or taxonomy tree, not for numbers.
  - Limits: at most 8 series of at most 200 points, at most 60 categories, at
    most 30 flow nodes; invalid blocks are dropped, not fixed. Chart titles,
    labels, and captions must be in English like the rest of the report.
  - Never force a chart: if the numbers are not comparable or a table reads
    better, write the table instead. A section needs at most one chart and the
    whole survey at most a handful.
- Self-contained: no internal stage/object names (see `schema/survey.md`).
