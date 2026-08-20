# JudgePanel Contract

Judges should be independent and evidence-oriented.

Each judge artifact returns:

- Exactly one standalone `verdict: <value>` line, where `<value>` is one of
  `strong`, `acceptable`, `insufficient`, or `blocked`.
- `evidence`: concrete observations from the current artifacts.
- `risks`: missing coverage, overclaim, benchmark mismatch, or speculative gaps.
- `required_fixes`: specific actions before final writing.
- `suggested_queries`: targeted retrieval queries.

The synthesizer artifact returns:

- Exactly one standalone `overall_verdict: <value>` line using the same enum.
- `what_is_ready`
- `what_must_be_caveated`
- `what_to_expand_next`
- `allowed_final_claims`
- `forbidden_overclaims`

Producers must continue emitting the canonical standalone form above. The runtime
normalizes only common Markdown, JSON/YAML, Unicode punctuation, and two-column
table decoration around that exact field. It still rejects prose matches,
unknown values, extra table cells, and duplicate verdict fields.
