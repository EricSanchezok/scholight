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
- `card_ids`: arXiv ids (present in `run_dir/cards/`) the section draws on.
- `transfer_angle`: a one-line cross-domain / anchor-transfer idea to weave in
  (may be empty).

A typical list: introduction, an evolution / research-arc section, one section
per method family, a synthesizing comparison, open problems, conclusion.

## Section file (expansion → application finalizer)

- Path: `run_dir/sections/<n>_<slug>.md`, starting with `## <title>`.
- Detailed, flowing prose grounded in the cards. Every cited paper must include
  its exact `[arxiv_id]`; an adjacent `(Author, year)` is optional. The application
  finalizer uses those identifiers to build one auditable reference list.
- Self-contained: no internal stage/object names (see `schema/survey.md`).
