import { Link } from "react-router-dom";

import styles from "../styles/app.module.css";

export function NotFoundPage() {
  return (
    <main className={styles.notFound}>
      <span>404</span>
      <h1>This page is outside the index.</h1>
      <p>The address may be outdated or incomplete.</p>
      <Link className={styles.primaryButton} to="/">
        Return home
      </Link>
    </main>
  );
}
