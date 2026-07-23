# Using Scholight

Scholight provides academic paper search through the web, a REST API, and a stateless MCP server.

## Deployment-relative URLs

Use the same origin as this document for every integration URL. For example, if this document was fetched from `https://papers.example.org/docs.md`, set:

```bash
export SCHOLIGHT_BASE_URL=https://papers.example.org
```

The public integration paths are:

- REST search: `/api/search`
- MCP server: `/api/mcp`
- OpenAPI schema: `/api/openapi.json`

Do not assume a fixed hostname. Scholight can be deployed at different public or private origins.

## Quick start

The REST search endpoint supports anonymous exploration:

```bash
curl -sS -X POST "$SCHOLIGHT_BASE_URL/api/search" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "retrieval augmented generation",
    "strength": "standard",
    "limit": 5,
    "filters": {
      "categories": ["cs.AI"]
    }
  }'
```

`query` should be a focused natural-language research question. Add filters only when they reflect the user's request.

## Authentication

Authentication is optional for public search. For integrations:

1. Sign in to Scholight and create an Access Key from the Access keys page.
2. Store the `sk_live_...` value in an environment variable or secret store.
3. Send it as a Bearer credential.

```bash
export SCHOLIGHT_API_KEY=sk_live_xxx

curl -sS -X POST "$SCHOLIGHT_BASE_URL/api/search" \
  -H "Authorization: Bearer $SCHOLIGHT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "vision language models",
    "strength": "thorough",
    "limit": 10
  }'
```

Never put an Access Key in browser code, source control, logs, or a URL. Web-login access tokens are not MCP credentials.

## REST search

Send `POST /api/search` with a JSON body.

The main request fields are:

- `query`: required natural-language research question.
- `strength`: `standard` by default, or `thorough`.
- `limit`: maximum number of ranked papers to return.
- `filters.categories`: optional category names.
- `filters.authors`: optional author names.
- `filters.date_from` and `filters.date_to`: optional inclusive submission-date range in `YYYY-MM-DD` format.

Use the OpenAPI schema at `/api/openapi.json` as the authoritative source for field types, validation constraints, and response models.

Preserve the returned `rank` order. A paper's `score` is an unnormalized ranking signal and should only be compared within the same response. A successful response with `degraded: true` means some metadata enrichment was unavailable; the ranked search itself still succeeded.

## MCP server

Connect an MCP client to the stateless Streamable HTTP endpoint:

```json
{
  "mcpServers": {
    "scholight": {
      "url": "https://YOUR_SCHOLIGHT_ORIGIN/api/mcp",
      "headers": {
        "Authorization": "Bearer sk_live_xxx"
      }
    }
  }
}
```

Replace `https://YOUR_SCHOLIGHT_ORIGIN` with the origin from which this document was fetched. The Authorization header may be omitted for anonymous searches.

The server exposes `search_papers`. Obtain its current tool and input descriptions through the MCP `tools/list` operation rather than copying a second schema from this document. Tool initialization and listing do not consume search quota.

## Choosing search strength

- `standard`: default for focused questions, iterative exploration, and workflows that may issue several searches.
- `thorough`: use when nuance and broader ranking are worth additional latency and quota.

Prefer a precise research question over disconnected keywords. Use author, category, and date filters only when the user explicitly needs those constraints.

## Errors and limits

- `400` or `422`: fix the request before retrying.
- `401` or `403`: create or replace the credential; do not retry the same invalid key.
- `429`: quota or rate limit reached; honor `Retry-After` when present.
- `503`: temporary service issue; retry with bounded exponential backoff.

Anonymous and authenticated limits may differ. Web and Access Key searches associated with the same user consume that user's Scholight quota.
