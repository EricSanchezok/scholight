---
name: scholight-search
description: Search Scholight's arXiv index for AI research papers. Use when a user asks to find papers, survey related work, compare research approaches, or filter academic results by arXiv category, author, or date.
---

# Scholight Search

Use the bundled standard-library CLI to search Scholight and receive the public API response as JSON.

## Choose Search Strength

- Use `standard` by default for quick discovery and straightforward topic searches.
- Use `thorough` when the question is nuanced, the first search is weak, or deeper ranking is worth extra latency and quota.

## Run a Search

Set `SCHOLIGHT_API_URL` to the public API base URL. Set `SCHOLIGHT_API_KEY` only when an Access Key is available; otherwise the search uses anonymous quota.

```bash
SCHOLIGHT_API_URL=https://example.com/api \
SCHOLIGHT_API_KEY=sk_live_xxx \
python3 <skill_dir>/scripts/search.py search \
  "retrieval augmented generation" \
  --strength standard \
  --limit 5 \
  --category cs.AI
```

Run `python3 <skill_dir>/scripts/search.py --help` or the `search --help` subcommand when parameters are unclear. Do not pass credentials on the command line or write them to a configuration file.

Use the returned rank as the authoritative ordering. If the response reports `degraded: true`, explain that some metadata enrichment was unavailable even though search results were returned.
