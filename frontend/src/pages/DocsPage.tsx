import { styles } from "../styles/classes";

type DocsSection = {
  title: string;
  body: string;
  code?: string;
};

const sections: DocsSection[] = [
  {
    title: "Getting started",
    body: "Enter a research topic, method, or question on the home page. Choose a search strength, then open a result on arXiv, read the PDF, or copy a citation.",
  },
  {
    title: "Writing a useful research query",
    body: "Describe the idea you are investigating in natural language. Include the task, domain, or comparison that matters. A focused question usually works better than a list of disconnected keywords.",
  },
  {
    title: "Standard vs Thorough",
    body: "Standard balances speed and result quality for everyday discovery. Thorough takes a deeper pass and may take longer; use it when nuance and breadth matter more than speed.",
  },
  {
    title: "Reading results and scores",
    body: "Results are ordered for the current search. Score is an unnormalized retrieval signal: compare it only among results from the same query, not across different searches or dates.",
  },
  {
    title: "Opening and citing papers",
    body: "The title and arXiv link open the paper’s abstract page. PDF opens the full paper. Cite copies a plain-text reference that you can paste into your notes.",
  },
  {
    title: "Search history and your account",
    body: "Anonymous search works without an account. When signed in, searches are added to your private history so you can run them again or remove them later. Account settings let you update your display name, review usage, and change your password.",
  },
  {
    title: "Limits and common errors",
    body: "Daily limits protect service quality. If a limit is reached, the message shows when to try again. Temporary search errors can be retried without changing your query. A partial-results notice means some abstracts were unavailable, but the visible papers are still usable.",
  },
  {
    title: "Search with curl",
    body: "Call the public search API anonymously, or add an Access Key created from your account. The OpenAPI schema remains the source of truth for request fields.",
    code: `curl -sS https://example.com/api/search \\
  -H 'Authorization: Bearer sk_live_xxx' \\
  -H 'Content-Type: application/json' \\
  -d '{"query":"retrieval augmented generation","strength":"standard","limit":5,"filters":{"categories":["cs.AI"]}}'`,
  },
  {
    title: "Connect an MCP client",
    body: "Scholight exposes one stateless search_papers tool. The Authorization header is optional for anonymous use and accepts only a Scholight Access Key when present.",
    code: `{
  "mcpServers": {
    "scholight": {
      "url": "https://example.com/api/mcp",
      "headers": { "Authorization": "Bearer sk_live_xxx" }
    }
  }
}`,
  },
  {
    title: "Use the Scholight Search Skill",
    body: "Install the Skill directly from the repository, set the API base URL, and let an agent call its dependency-free JSON CLI.",
    code: `SCHOLIGHT_API_URL=https://example.com/api \\
SCHOLIGHT_API_KEY=sk_live_xxx \\
python3 <skill_dir>/scripts/search.py search \\
  "retrieval augmented generation" --strength standard --limit 5 --category cs.AI`,
  },
];

export function DocsPage() {
  return (
    <main className={styles.docs}>
      <header>
        <div className={styles.accentLine} />
        <p className={styles.eyebrow}>Documentation</p>
        <h1>Using Scholight</h1>
        <p>Find, evaluate, and return to academic research with a focused search workflow.</p>
      </header>
      <div className={styles.docSections}>
        {sections.map(({ title, body, code }, index) => (
          <section key={title}>
            <span>{String(index + 1).padStart(2, "0")}</span>
            <div>
              <h2>{title}</h2>
              <p>{body}</p>
              {code ? (
                <pre>
                  <code>{code}</code>
                </pre>
              ) : null}
            </div>
          </section>
        ))}
      </div>
    </main>
  );
}
