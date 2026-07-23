import type { DeploymentUrls } from "../../config/runtime";

export function buildDocsExamples(urls: DeploymentUrls) {
  return {
    anonymousCurl: `curl -sS -X POST ${urls.search} \\
  -H 'Content-Type: application/json' \\
  -d '{
    "query": "retrieval augmented generation",
    "strength": "standard",
    "limit": 5,
    "filters": { "categories": ["cs.AI"] }
  }'`,
    authenticatedCurl: `curl -sS -X POST ${urls.search} \\
  -H 'Authorization: Bearer sk_live_xxx' \\
  -H 'Content-Type: application/json' \\
  -d '{"query":"vision language models","strength":"thorough","limit":10}'`,
    mcp: `{
  "mcpServers": {
    "scholight": {
      "url": "${urls.mcp}",
      "headers": {
        "Authorization": "Bearer sk_live_xxx"
      }
    }
  }
}`,
    skillSearch: `SCHOLIGHT_API_URL=${urls.api} \\
SCHOLIGHT_API_KEY=sk_live_xxx \\
python3 <skill_dir>/scripts/search.py search \\
  "retrieval augmented generation" \\
  --strength standard \\
  --limit 5 \\
  --category cs.AI`,
    response: `{
  "query": "retrieval augmented generation",
  "strength": "standard",
  "degraded": false,
  "hits": [
    {
      "rank": 1,
      "score": 12.75,
      "arxiv_id": "2401.12345",
      "title": "A Paper About Retrieval",
      "authors": ["Example Author"],
      "categories": ["cs.AI", "cs.IR"],
      "arxiv_url": "https://arxiv.org/abs/2401.12345",
      "pdf_url": "https://arxiv.org/pdf/2401.12345"
    }
  ],
  "result_count": 1,
  "elapsed_ms": 842.37
}`,
  };
}
