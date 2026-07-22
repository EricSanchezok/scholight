import { SearchForm } from "../components/SearchForm";
import styles from "../styles/app.module.css";

export function HomePage() {
  return (
    <main className={styles.home}>
      <section className={styles.hero}>
        <div className={styles.accentLine} />
        <h1>
          Academic search, built for <span>AI.</span>
        </h1>
        <p>Find the research that matters to your work.</p>
        <SearchForm />
      </section>
    </main>
  );
}
