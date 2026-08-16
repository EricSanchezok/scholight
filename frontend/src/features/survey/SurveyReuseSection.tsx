import { styles } from "../../styles/classes";

export function SurveyReuseSection({
  busy,
  error,
  onReuse,
}: {
  busy: boolean;
  error: string | undefined;
  onReuse: () => void;
}) {
  return (
    <section className={styles.surveyReuseSection}>
      <h2>Continue this research</h2>
      <p>
        Prepare a new draft from the original request. This previous record will stay unchanged for
        reference.
      </p>
      <button type="button" className={styles.primaryButton} disabled={busy} onClick={onReuse}>
        {busy ? "Preparing…" : "Use request again"}
      </button>
      {error && (
        <p className={styles.surveyInlineError} role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
