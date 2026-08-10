# CardPlan Contract

`CardPlan` selects which papers the per-paper `paper_cards` stage reads in full.
It is the budget gate between the ranked pool and full-text reading.

## Constant

- `max_fulltext_papers = 100` — the maximum number of papers the plan may list.
  Reading full text is the expensive step, so this caps cost. Tune the budget
  here, in the contract — not by editing the prompt text.

## Durable output

Before dispatching workers, the planner writes a JSON array to
`run_dir/00_card_plan.json`, one element per selected paper. The spawn call uses
the same entries; the file preserves expectations across retries and recovery:

- `run_dir`: the run directory, verbatim, in every element.
- `id`: the canonical semantic arXiv id — must exist in the ranked pool; never
  invented. Legacy ids retain their slash here (for example `cs/0012009`).
- `title`: the paper title.
- `why`: one line — its role + why it matters to the anchor. Tag cross-domain
  picks with "cross_domain transfer".

When an id is used in a local artifact filename, replace the single slash in a
legacy id with `-`. Thus `cs/0012009` maps to `cards/cs-0012009.md`; the semantic
id remains `cs/0012009` in metadata, retrieval, and citations.

## Selection priority

Within `max_fulltext_papers`, prefer: core_set / method papers with strong
topic_fit; especially `abstract_only` ones (their bodies are unread — the whole
point); a few already-`full_text` anchors (re-read through the anchor lens); and
several `transfer_set` (role: cross_domain) candidates with strong
`transfer_potential`. Skip in-domain boundary / low-topic_fit / off-scope papers —
but transfer_set candidates are not off-scope; they have their own lane.
