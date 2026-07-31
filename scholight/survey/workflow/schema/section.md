# Section Contract

The survey is built **section by section**: the outline node records the macro
structure, dispatches one work item per section, and the assembler stitches the
resulting files into the final survey. This keeps each writing step's context
small while letting every section go deep.

## Section dispatch (outline → expansion)

The outline writes the narrative plan to `run_dir/00_outline.md`, then calls
`spawn_SectionExpander` with one item per section in reading order. Each item
contains:

- `run_dir`: the run directory, repeated verbatim in every item.
- `n`: two-digit order string, e.g. `"01"` — controls file name and final order.
- `slug`: short kebab ID, e.g. `"feedback-alignment"`.
- `title`: the section heading.
- `thesis`: one-line claim the section argues (its part of the through-line).
- `card_ids`: arXiv IDs present in `run_dir/cards/` that the section draws on.
- `transfer_angle`: a one-line cross-domain / anchor-transfer idea to weave in
  (may be empty).

A typical list: introduction, an evolution / research-arc section, one section
per method family, a synthesizing comparison, open problems, conclusion.

Use `max_parallel=10`. If a work item fails, retry only that failed item rather
than dispatching successful sections again.

## Section file (expansion → assembler)

- Path: `run_dir/sections/<n>_<slug>.md`, starting with `## <title>`.
- Detailed, flowing prose grounded in the cards; cites inline by `(Author, year)`
  or `[arxiv_id]`. The assembler builds the single reference list.
- Self-contained: no internal stage/object names (see `schema/survey.md`).
