import { styles } from "../styles/classes";

const sections = [
  [
    "Getting started",
    "Enter a research topic, method, or question on the home page. Choose a search strength, then open a result on arXiv, read the PDF, or copy a citation.",
  ],
  [
    "Writing a useful research query",
    "Describe the idea you are investigating in natural language. Include the task, domain, or comparison that matters. A focused question usually works better than a list of disconnected keywords.",
  ],
  [
    "Standard vs Thorough",
    "Standard balances speed and result quality for everyday discovery. Thorough takes a deeper pass and may take longer; use it when nuance and breadth matter more than speed.",
  ],
  [
    "Reading results and scores",
    "Results are ordered for the current search. Score is an unnormalized retrieval signal: compare it only among results from the same query, not across different searches or dates.",
  ],
  [
    "Opening and citing papers",
    "The title and arXiv link open the paper’s abstract page. PDF opens the full paper. Cite copies a plain-text reference that you can paste into your notes.",
  ],
  [
    "Search history and your account",
    "Anonymous search works without an account. When signed in, searches are added to your private history so you can run them again or remove them later. Account settings let you update your display name, review usage, and change your password.",
  ],
  [
    "Limits and common errors",
    "Daily limits protect service quality. If a limit is reached, the message shows when to try again. Temporary search errors can be retried without changing your query. A partial-results notice means some abstracts were unavailable, but the visible papers are still usable.",
  ],
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
        {sections.map(([title, body], index) => (
          <section key={title}>
            <span>{String(index + 1).padStart(2, "0")}</span>
            <div>
              <h2>{title}</h2>
              <p>{body}</p>
            </div>
          </section>
        ))}
      </div>
    </main>
  );
}
