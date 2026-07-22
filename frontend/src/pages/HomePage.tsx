import { SearchForm } from "../components/SearchForm";
import styles from "../styles/app.module.css";

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
        <SearchForm />
      </section>
    </main>
  );
}
