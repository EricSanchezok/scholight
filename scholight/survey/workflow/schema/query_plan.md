# QueryPlan Contract

`QueryPlan` turns `SurveySpec` into a typed retrieval program.

Each query must include:

- `query_id`
- `query_type`: core_method, mechanism, problem, benchmark, survey, citation_seed, boundary, frontier, or cross_domain.
- `query`
- `target_dimension`
- `expected_gain`
- `negative_filter`
- `top_k`

The plan must include at least:

- 5 core or mechanism queries.
- 3 benchmark or evaluation queries.
- 2 survey or review queries.
- 2 boundary queries.
- 2 frontier queries.
- 2 cross-domain queries.

Queries should be focused natural-language requests suitable for Scholight
search, while remaining diverse enough to expose adjacent methods, evidence,
and boundary cases.
