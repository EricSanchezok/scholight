import { CopyCodeBlock } from "../components/CopyCodeBlock";
import { buildDeploymentUrls, runtimeConfig } from "../config/runtime";
import { buildDocsExamples, scholightRepositoryUrl } from "../features/docs/examples";
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
      ["Search Skill", "#search-skill"],
    ],
  },
  {
    label: "Reference",
    links: [
      ["Search behavior", "#search-behavior"],
      ["Errors and limits", "#errors-limits"],
      ["Deployment URLs", "#deployment-urls"],
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
        <h1>Build with Scholight</h1>
        <p>
          Search Scholight&apos;s arXiv index from the web, a REST client, an MCP agent, or the
          dependency-free Search Skill.
        </p>
        <dl className={styles.deploymentAddress}>
          <div>
            <dt>This deployment</dt>
            <dd>{urls.web}</dd>
          </div>
          <div>
            <dt>API base URL</dt>
            <dd>{urls.api}</dd>
          </div>
        </dl>
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
            <div className={styles.docsCallout}>
              <strong>Live address.</strong>
              <p>
                This example already uses <code>{urls.search}</code>, the search endpoint visible
                from the Scholight instance you are reading now.
              </p>
            </div>
          </section>

          <section className={styles.docsSection} id="authentication">
            <p className={styles.docsSectionLabel}>02 · Authentication</p>
            <h2>Anonymous when exploring, Access Key when integrating</h2>
            <p className={styles.docsLead}>
              Omit the Authorization header for anonymous use. For tools and agents, create an
              Access Key in your account and send it as a Bearer credential.
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
            <h2>One stable public search contract</h2>
            <p className={styles.docsLead}>
              Request and response validation comes from the server&apos;s OpenAPI schema. The links
              below always point to this deployment, so field definitions cannot drift into a second
              handwritten reference.
            </p>
            <div className={styles.endpointTable}>
              <EndpointRow
                label="Search papers"
                method="POST"
                url={urls.search}
                description="Anonymous or Access Key"
              />
              <EndpointRow
                label="OpenAPI schema"
                method="GET"
                url={urls.openapi}
                description="Machine-readable contract"
              />
              <EndpointRow
                label="Interactive API"
                method="GET"
                url={urls.interactiveApi}
                description="Try requests in the browser"
              />
            </div>
            <p className={styles.docsLinks}>
              <a href={urls.interactiveApi} target="_blank" rel="noreferrer">
                Open interactive API docs
              </a>
              <a href={urls.openapi} target="_blank" rel="noreferrer">
                View OpenAPI JSON
              </a>
            </p>
            <h3>Response essentials</h3>
            <p>
              Use <code>rank</code> as the authoritative order. The <code>score</code> is an
              unnormalized signal that is comparable only inside the same response. A successful
              response with <code>degraded: true</code> means some metadata enrichment was
              unavailable, not that the ranked search failed.
            </p>
            <CopyCodeBlock code={code.response} language="json" />
          </section>

          <section className={styles.docsSection} id="mcp-server">
            <p className={styles.docsSectionLabel}>04 · MCP server</p>
            <h2>Connect an agent over Streamable HTTP</h2>
            <p className={styles.docsLead}>
              Scholight exposes a stateless MCP server at <code>{urls.mcp}</code>. Native clients
              can connect directly; the Authorization header is optional for anonymous searches.
            </p>
            <CopyCodeBlock code={code.mcp} language="json" />
            <div className={styles.docsSplit}>
              <div>
                <h3>Tool</h3>
                <p>
                  <code>search_papers</code> returns concise Markdown and structured content that
                  matches the REST response.
                </p>
              </div>
              <div>
                <h3>Inputs</h3>
                <p>
                  <code>query</code>, <code>strength</code>, <code>limit</code>, categories,
                  authors, and an optional date range.
                </p>
              </div>
            </div>
            <p className={styles.docsNote}>
              MCP clients use different names for their top-level configuration object. Preserve the
              URL and optional Authorization header above if your client uses a different wrapper.
            </p>
          </section>

          <section className={styles.docsSection} id="search-skill">
            <p className={styles.docsSectionLabel}>05 · Search Skill</p>
            <h2>Give an agent a small, inspectable CLI</h2>
            <p className={styles.docsLead}>
              The repository includes a Skill with a Python standard-library CLI. Install that
              directory into your agent&apos;s Skill location, then point it at this API base.
            </p>
            <h3>Download and install</h3>
            <CopyCodeBlock code={code.skillInstall} language="bash" />
            <div className={styles.skillLocations}>
              <div>
                <span>Codex</span>
                <code>~/.codex/skills/scholight-search</code>
              </div>
              <div>
                <span>Claude Code</span>
                <code>~/.claude/skills/scholight-search</code>
              </div>
              <div>
                <span>Cursor / Windsurf</span>
                <code>&lt;project&gt;/.skills/scholight-search</code>
              </div>
              <div>
                <span>Other agents</span>
                <code>&lt;agent_skill_dir&gt;/scholight-search</code>
              </div>
            </div>
            <h3>Verify with a search</h3>
            <CopyCodeBlock code={code.skillSearch} language="bash" />
            <p className={styles.docsLinks}>
              <a href={`${scholightRepositoryUrl}/tree/main/skills/scholight-search`}>
                Inspect the Skill source
              </a>
            </p>
          </section>

          <section className={styles.docsSection} id="search-behavior">
            <p className={styles.docsSectionLabel}>06 · Search behavior</p>
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
              Scholight searches AI research from arXiv. A focused natural-language question with a
              task, method, or comparison usually performs better than disconnected keywords.
            </p>
          </section>

          <section className={styles.docsSection} id="errors-limits">
            <p className={styles.docsSectionLabel}>07 · Errors and limits</p>
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
              recent usage; initialize and tool-list MCP requests do not consume search quota.
            </p>
          </section>

          <section className={styles.docsSection} id="deployment-urls">
            <p className={styles.docsSectionLabel}>08 · Deployment URLs</p>
            <h2>One frontend build, different reachable addresses</h2>
            <p className={styles.docsLead}>
              Every address on this page is derived at runtime from the URL in the browser plus the
              shared <code>/api</code> proxy path. The same image can therefore serve a public
              domain, an internal cluster hostname, and localhost without rebuilding the Docs page.
            </p>
            <div className={styles.endpointTable}>
              <EndpointRow
                label="Web"
                method="CURRENT"
                url={urls.web}
                description="Browser-visible origin"
              />
              <EndpointRow
                label="REST base"
                method="DERIVED"
                url={urls.api}
                description="Current origin plus /api"
              />
              <EndpointRow
                label="MCP"
                method="DERIVED"
                url={urls.mcp}
                description="Current API base plus /mcp"
              />
            </div>
            <div className={styles.docsCallout}>
              <strong>Keep the proxy contract.</strong>
              <p>
                Each deployment should expose its own backend under <code>/api</code> on the same
                origin as its frontend. Do not publish container-only hostnames in browser
                configuration.
              </p>
            </div>
            <h3>Email links are configured separately</h3>
            <p>
              Set <code>SCHOLIGHT_PUBLIC_WEB_URL</code> per API instance to the address that users
              of that instance can open from verification and password-reset email. The backend does
              not infer this value from forwarded headers.
            </p>
          </section>
        </div>
      </div>
    </main>
  );
}
