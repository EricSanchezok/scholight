# Expansion Contract

`ExpansionReport` records three expansion routes:

- citation graph expansion from seed PDFs and references.
- semantic-neighbor expansion using Scholight search.
- cross-domain transfer mining: abstract our methods into domain-agnostic
  patterns, then search OTHER fields for transferable work.

Required sections:

- `seed_papers`: selected papers and why they were selected.
- `citation_edges`: source paper to referenced paper or title.
- `resolved_references`: references resolved through Scholight search.
- `semantic_neighbors`: Scholight search neighbors grouped by expansion query.
- `patterns`: domain-agnostic abstractions of our methods (the transferable core
  of each, stated without our field's vocabulary), with search keywords and the
  method each was abstracted from.
- `cross_domain_candidates`: out-of-field papers found via the patterns. For each:
  `arxiv_id`, `title`, `source_domain` (the category or field reported by
  Scholight),
  `matched_pattern`, `transfer_hypothesis` (how it might transfer to our anchor),
  and `role: cross_domain`. Intentionally out of scope — kept in a separate
  transfer lane, not the in-domain pool.
- `new_candidates`: deduplicated additions.
- `drift_risks`: papers or clusters likely outside scope.
- `next_expansion_queries`: targeted follow-up queries.

Citation links may be partial. Mark unknowns explicitly instead of fabricating IDs.
