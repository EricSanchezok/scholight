# CardPlan Contract

`CardPlan` selects which papers the `PaperCard` workers read in full. It is the
budget gate between the ranked pool and full-text reading.

## Constant

- `max_fulltext_papers = 100` — the maximum number of papers the plan may list.
  Reading full text is the expensive step, so this caps cost. Tune the budget
  here, in the contract — not by editing the prompt text.

## Dispatch

The planner calls `spawn_PaperCard` with one item per selected paper. It does not
write an intermediate scatter file. Each item contains:

- `run_dir`: the run directory, verbatim, in every item.
- `id`: the paper's arXiv ID as returned in the ranked pool; never invented.
- `title`: the paper title.
- `why`: one line — its role + why it matters to the anchor. Tag cross-domain
  picks with "cross_domain transfer".

Use `max_parallel=20`. If a work item fails, retry only that failed item rather
than dispatching successful papers again.

## Selection priority

Within `max_fulltext_papers`, prefer: core_set / method papers with strong
topic_fit; especially `abstract_only` ones (their bodies are unread — the whole
point); a few already-`full_text` anchors (re-read through the anchor lens); and
several `transfer_set` (role: cross_domain) candidates with strong
`transfer_potential`. Skip in-domain boundary / low-topic_fit / off-scope papers —
but transfer_set candidates are not off-scope; they have their own lane.
