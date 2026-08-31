import { Link } from "react-router-dom";

import { ProductMark } from "../brand/ProductMark";
import { routes, withQuery } from "../app/routes";
import { SearchForm } from "../components/SearchForm";
import { styles } from "../styles/classes";

export function HomePage() {
  return (
    <main className={styles.home}>
      <section className={styles.hero}>
        <div className={styles.accentLine} />
        <div className={styles.heroCopy}>
          <h1>
            <span>Academic search,</span>
            <span>
              built for <em>AI.</em>
            </span>
          </h1>
          <p>Find the research that matters to your work.</p>
        </div>
        <div className={styles.heroArtFrame}>
          <ProductMark size="clamp(220px, 34vw, 420px)" priority />
          <p className={styles.homeHeroNote}>A quiet lens on the literature.</p>
        </div>
        <SearchForm />
        <div className={styles.homeExamples} aria-label="Search examples">
          <span>Try a question</span>
          <Link
            className={styles.homeExample}
            to={withQuery(routes.search.path, {
              q: "vision transformers",
              strength: "standard",
            })}
          >
            How do vision transformers work?
          </Link>
          <Link
            className={styles.homeExample}
            to={withQuery(routes.search.path, {
              q: "retrieval augmented generation",
              strength: "thorough",
            })}
          >
            Retrieval-augmented generation methods
          </Link>
        </div>
      </section>

      <section className={styles.homeSections} aria-label="Scholight capabilities">
        <div className={styles.homeSection}>
          <div className={styles.homeSectionIntro}>
            <p className={styles.eyebrow}>Search with intent</p>
            <h2>Start with the question, not the keywords.</h2>
            <p>
              Scholight turns a natural-language research question into a focused reading list, with
              enough context to decide what deserves your attention next.
            </p>
          </div>
          <div className={styles.homeFeatureGrid}>
            <article className={styles.homeFeature}>
              <span>01</span>
              <h3>Standard</h3>
              <p>Fast orientation when you are opening a new line of inquiry.</p>
            </article>
            <article className={styles.homeFeature}>
              <span>02</span>
              <h3>Thorough</h3>
              <p>Deeper coverage when the question needs a more considered pass.</p>
            </article>
            <article className={styles.homeFeature}>
              <span>03</span>
              <h3>Readable results</h3>
              <p>Titles, authors, abstracts and source links stay together for scanning.</p>
            </article>
          </div>
        </div>

        <div className={styles.homeSection}>
          <div className={styles.homeSectionIntro}>
            <p className={styles.eyebrow}>For repeatable work</p>
            <h2>Keep the search close to the rest of your workflow.</h2>
            <p>
              Return to useful searches, monitor your usage, and connect tools when a browser is no
              longer the right surface.
            </p>
          </div>
          <div className={styles.homeFeatureGrid}>
            <article className={styles.homeFeature}>
              <span>History</span>
              <h3>Pick up where you left off</h3>
              <p>Save the path from a research question to the papers worth revisiting.</p>
              <Link to={routes.history.path}>View history</Link>
            </article>
            <article className={styles.homeFeature}>
              <span>REST + MCP</span>
              <h3>Bring search to your tools</h3>
              <p>Use an Access Key when an agent or service needs the same research surface.</p>
              <Link to={routes.docs.path}>Read the docs</Link>
            </article>
            <article className={styles.homeFeature}>
              <span>Source</span>
              <h3>Stay close to the paper</h3>
              <p>Search the current arXiv corpus and follow each result to its source.</p>
              <Link to={routes.search.path}>Explore search</Link>
            </article>
          </div>
        </div>

        <div className={styles.homeCta}>
          <div>
            <p className={styles.eyebrow}>A better research starting point</p>
            <h2>Open a question and see where it leads.</h2>
          </div>
          <div className={styles.homeCtaActions}>
            <Link className={styles.primaryButton} to={routes.search.path}>
              Search papers
            </Link>
            <Link className={styles.secondaryButton} to={routes.docs.path}>
              Read documentation
            </Link>
          </div>
        </div>
      </section>
    </main>
  );
}
