import { CopyCodeBlock } from "../components/CopyCodeBlock";
import { buildDeploymentUrls, runtimeConfig } from "../config/runtime";
import { buildDocsExamples } from "../features/docs/examples";
import { styles } from "../styles/classes";
import { routes } from "../app/routes";

export { buildDeploymentUrls } from "../config/runtime";

type DocsPageProps = {
  origin?: string;
};

type EndpointRowProps = {
  label: string;
  method: string;
  url: string;
  description: string;
};

const navigation = [
  {
    label: "Get started",
    links: [
      ["Quick start", "#quick-start"],
      ["Authentication", "#authentication"],
    ],
  },
  {
    label: "Integrate",
    links: [
      ["REST API", "#rest-api"],
      ["MCP server", "#mcp-server"],
    ],
  },
  {
    label: "Reference",
    links: [
      ["Search behavior", "#search-behavior"],
      ["Errors and limits", "#errors-limits"],
    ],
  },
] as const;

function EndpointRow({ label, method, url, description }: EndpointRowProps) {
  return (
    <div className={styles.endpointRow}>
      <div>
        <strong>{label}</strong>
        <span>{description}</span>
      </div>
      <span>{method}</span>
      <code>{url}</code>
    </div>
  );
}

export function DocsPage({ origin }: DocsPageProps = {}) {
  const urls = buildDeploymentUrls(origin ?? window.location.origin, runtimeConfig.apiBasePath);
  const code = buildDocsExamples(urls);

  return (
    <main className={styles.docs}>
      <header className={styles.docsHero}>
        <div className={styles.accentLine} />
        <p className={styles.eyebrow}>Documentation</p>
        <h1>Using Scholight</h1>
        <p>
          Search academic literature and retrieve readable source content from REST or an
          MCP-enabled agent.
        </p>
      </header>

      <div className={styles.docsLayout}>
        <aside className={styles.docsSidebar}>
          <nav aria-label="Documentation">
            {navigation.map((group) => (
              <div className={styles.docsNavGroup} key={group.label}>
                <p>{group.label}</p>
                {group.links.map(([label, href]) => (
                  <a href={href} key={href}>
                    {label}
                  </a>
                ))}
              </div>
            ))}
          </nav>
        </aside>

        <div className={styles.docsContent}>
          <section className={styles.docsSection} id="quick-start">
            <p className={styles.docsSectionLabel}>01 · Quick start</p>
            <h2>Your first paper search</h2>
            <p className={styles.docsLead}>
              The public search endpoint works without an account. Send a natural-language query and
              add filters only when they improve the question.
            </p>
            <CopyCodeBlock code={code.anonymousCurl} language="bash" />
          </section>

          <section className={styles.docsSection} id="authentication">
            <p className={styles.docsSectionLabel}>02 · Authentication</p>
            <h2>Anonymous when exploring, Access Key when integrating</h2>
            <p className={styles.docsLead}>
              Omit the Authorization header for anonymous use. For tools and agents, create an
              Access Key in your account and send it as a Bearer credential. The same key works
              across every current and future Scholight tool.
            </p>
            <ol className={styles.docsSteps}>
              <li>
                <span>1</span>
                <div>
                  <strong>Create a key</strong>
                  <p>
                    Open <a href={routes.accessKeys.path}>Access keys</a>, choose a descriptive
                    name, and copy the secret when it appears. It is shown once.
                  </p>
                </div>
              </li>
              <li>
                <span>2</span>
                <div>
                  <strong>Store it as a secret</strong>
                  <p>
                    Keep <code>sk_live_…</code> in an environment variable or server-side secret
                    store, never in browser code or source control.
                  </p>
                </div>
              </li>
              <li>
                <span>3</span>
                <div>
                  <strong>Send the Bearer header</strong>
                  <p>
                    MCP accepts only Scholight Access Keys. A web login access token is not an MCP
                    credential.
                  </p>
                </div>
              </li>
            </ol>
            <CopyCodeBlock code={code.authenticatedCurl} language="bash" />
          </section>

          <section className={styles.docsSection} id="rest-api">
            <p className={styles.docsSectionLabel}>03 · REST API</p>
            <h2>Search papers and extract source content</h2>
            <p className={styles.docsLead}>
              Send a JSON request to the search endpoint. Authentication is optional; add an Access
              Key when you want the search associated with your account and authenticated quota.
            </p>
            <div className={styles.endpointTable}>
              <EndpointRow
                label="Search papers"
                method="POST"
                url={urls.search}
                description="Anonymous or Access Key"
              />
              <EndpointRow
                label="Extract URL"
                method="POST"
                url={urls.extract}
                description="Access Key required"
              />
            </div>
            <div className={styles.docsSplit}>
              <div>
                <h3>Required</h3>
                <p>
                  <code>query</code> is a natural-language research question. It accepts between 1
                  and 500 characters.
                </p>
              </div>
              <div>
                <h3>Optional</h3>
                <p>
                  Choose <code>strength</code> and <code>limit</code>, then filter by arXiv
                  categories, authors, or submission dates when useful.
                </p>
              </div>
            </div>
            <h3>Response essentials</h3>
            <p>
              Use <code>rank</code> as the authoritative order. The <code>score</code> is an
              unnormalized signal that is comparable only inside the same response. A successful
              response with <code>degraded: true</code> means some metadata enrichment was
              unavailable, not that the ranked search failed.
            </p>
            <CopyCodeBlock code={code.response} language="json" />
            <h3>Extract a URL</h3>
            <p>
              Web Extract accepts public HTTP and HTTPS URLs, including non-default ports. It can
              send target headers or stateless cookies, automatically render JavaScript when needed,
              and return main Markdown, full Markdown, text, or raw HTML. JSON, XML, and PDF
              documents are handled directly. Use <code>next_cursor</code> to continue a long,
              immutable result without fetching the source again.
            </p>
            <CopyCodeBlock code={code.extractCurl} language="bash" />
          </section>

          <section className={styles.docsSection} id="mcp-server">
            <p className={styles.docsSectionLabel}>04 · MCP server</p>
            <h2>Connect an agent over Streamable HTTP</h2>
            <p className={styles.docsLead}>
              Scholight exposes a stateless MCP server at <code>{urls.mcp}</code>. Native clients
              can connect directly; the Authorization header is optional for anonymous searches and
              required for Web Extract.
            </p>
            <CopyCodeBlock code={code.mcp} language="json" />
            <div className={styles.docsSplit}>
              <div>
                <h3>Paper search</h3>
                <p>
                  <code>search_papers</code> returns concise Markdown and structured content that
                  matches the REST response.
                </p>
              </div>
              <div>
                <h3>Web extraction</h3>
                <p>
                  <code>extract_url</code> retrieves readable content from a URL and returns both
                  concise Markdown and the structured REST-compatible response.
                </p>
              </div>
            </div>
            <p className={styles.docsNote}>
              MCP clients use different names for their top-level configuration object. Preserve the
              URL and optional Authorization header above if your client uses a different wrapper.
            </p>
          </section>

          <section className={styles.docsSection} id="search-behavior">
            <p className={styles.docsSectionLabel}>05 · Search behavior</p>
            <h2>Choose depth deliberately</h2>
            <div className={styles.docsSplit}>
              <div>
                <p className={styles.docsChoice}>Standard</p>
                <h3>Fast discovery</h3>
                <p>
                  The default for focused questions, iterative exploration, and agent workflows that
                  may issue several searches.
                </p>
              </div>
              <div>
                <p className={styles.docsChoice}>Thorough</p>
                <h3>Deeper ranking</h3>
                <p>
                  Use when nuance and breadth are worth additional latency and quota. It is not a
                  substitute for a more precise query.
                </p>
              </div>
            </div>
            <p>
              Scholight currently indexes AI research from arXiv and keeps its corpus boundary open
              to additional scholarly sources. A focused question with a task, method, or comparison
              usually performs better than disconnected keywords.
            </p>
          </section>

          <section className={styles.docsSection} id="errors-limits">
            <p className={styles.docsSectionLabel}>06 · Errors and limits</p>
            <h2>Handle failures by category</h2>
            <div className={styles.endpointTable}>
              <EndpointRow
                label="Invalid request"
                method="400 / 422"
                url="Check the response detail"
                description="Fix input before retrying"
              />
              <EndpointRow
                label="Invalid key"
                method="401 / 403"
                url="Create or replace the Access Key"
                description="Do not retry the same credential"
              />
              <EndpointRow
                label="Quota or rate limit"
                method="429"
                url="Honor Retry-After when present"
                description="Retry later"
              />
              <EndpointRow
                label="Temporary service issue"
                method="503"
                url="Use bounded backoff"
                description="Safe to retry"
              />
            </div>
            <p className={styles.docsNote}>
              Anonymous and authenticated daily limits differ. Sign in to review current quota and
              recent search usage. Web Extract has no daily quota; it uses bounded response sizes,
              deadlines, and concurrency. Initialize and tool-list MCP requests consume no quota.
            </p>
          </section>
        </div>
      </div>
    </main>
  );
}
