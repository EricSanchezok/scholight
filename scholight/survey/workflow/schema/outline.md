# Structured Survey Outline Contract

The outline stage writes `run_dir/00_outline.json` before its human-readable
Markdown outline. This small JSON object is the machine contract used by the
application finalizer:

```json
{
  "schema_version": 1,
  "title": "A concise survey title",
  "abstract": "A four-to-six sentence abstract in the requested language.",
  "through_line": "The central argument every section advances."
}
```

All four fields are required. `schema_version` is the integer `1`; `title`,
`abstract`, and `through_line` are non-empty strings. The title must be plain
text rather than a Markdown heading. The Markdown `00_outline.md` must express
the same title, abstract, and through-line while also carrying the ordered
section plan for human review.
