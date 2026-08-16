import type { SurveyDraft } from "../../api/types";
import { formatRelativeTime } from "../../i18n/format";
import type { AppLocale } from "../../i18n/I18nProvider";
import { styles } from "../../styles/classes";

export function SurveyDraftHistory({
  drafts,
  currentId,
  selectedId,
  initialRequest,
  readOnly,
  locale,
  onSelect,
}: {
  drafts: SurveyDraft[];
  currentId: string | undefined;
  selectedId: string | undefined;
  initialRequest: string;
  readOnly: boolean;
  locale: AppLocale;
  onSelect: (draftId: string) => void;
}) {
  const versions = drafts
    .filter((draft) => draft.status === "ready" && draft.revision !== null)
    .sort((a, b) => (b.revision ?? 0) - (a.revision ?? 0));
  return (
    <aside className={styles.surveyDraftHistory}>
      <details className={styles.surveyOriginalRequest} open={readOnly || undefined}>
        <summary>Original request</summary>
        <p>{initialRequest}</p>
      </details>
      <h2>Draft history</h2>
      <p>Each version includes the draft and the feedback used to revise it.</p>
      {!versions.length ? (
        <div className={styles.surveyDraftHistoryEmpty}>
          Your first draft will appear here when it is ready.
        </div>
      ) : (
        <ol>
          {versions.map((draft) => {
            const selected = (selectedId ?? currentId) === draft.id;
            return (
              <li className={selected ? styles.surveyDraftHistorySelected : ""} key={draft.id}>
                <div>
                  <strong>v{draft.revision}</strong>
                  {draft.id === currentId && <span>CURRENT</span>}
                </div>
                <blockquote>“{draft.user_message}”</blockquote>
                <button type="button" onClick={() => onSelect(draft.id)}>
                  {formatRelativeTime(draft.finished_at ?? draft.created_at, locale)} · View draft
                </button>
              </li>
            );
          })}
        </ol>
      )}
      <small>
        {readOnly
          ? "This record is read-only. Its drafts and original request remain available."
          : "Revisions stop at v10. You can still review, edit, or approve the current draft."}
      </small>
    </aside>
  );
}
