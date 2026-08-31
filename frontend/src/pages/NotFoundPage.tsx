import { Link } from "react-router-dom";

import { ProductMark } from "../brand/ProductMark";
import { styles } from "../styles/classes";
import { routes } from "../app/routes";

export function NotFoundPage() {
  return (
    <main className={styles.notFound}>
      <ProductMark className={styles.notFoundMark} size={112} decorative priority />
      <span>404</span>
      <h1>This page is outside the index.</h1>
      <p>The address may be outdated or incomplete.</p>
      <Link className={styles.primaryButton} to={routes.home.path}>
        Return home
      </Link>
    </main>
  );
}
