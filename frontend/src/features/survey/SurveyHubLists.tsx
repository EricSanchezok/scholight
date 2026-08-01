import { Link } from "react-router-dom";

import type { SurveySummary } from "../../api/types";
import { surveyDraftPath, surveyReportPath } from "../../app/routes";
import { formatDurationBetween, formatRelativeTime, formatReportDate } from "../../i18n/format";
import type { AppLocale } from "../../i18n/I18nProvider";
import { styles } from "../../styles/classes";
import { queueDescription, runningDescription, surveyStageLabel } from "./survey";

export function ActiveSurveyList({
  items,
  locale,
  onCancel,
}: {
  items: SurveySummary[];
  locale: AppLocale;
  onCancel: (survey: SurveySummary) => void;
}) {
  if (!items.length) {
    return (
      <div className={styles.surveyEmpty}>
        <h2>No surveys are running</h2>
        <p>Describe a research question above to prepare a new survey.</p>
      </div>
    );
  }

  return (
    <div className={styles.surveyActiveList} aria-live="polite">
      {items.map((survey) => {
        const progress = survey.progress;
        const isDraft = survey.status === "drafting";
        const isQueued = progress.stage === "waiting_for_execution";
        const isDraftQueued = progress.stage === "waiting_for_draft";
        const isRunning = !isDraft && !isQueued;
        const metadata =
          isDraftQueued || isQueued
            ? queueDescription(progress, locale)
            : isDraft
              ? survey.latest_draft_revision
                ? `Draft v${survey.latest_draft_revision} of 10  ·  Updated ${formatRelativeTime(survey.updated_at, locale)}`
                : `${surveyStageLabel(progress.stage)}  ·  Updated ${formatRelativeTime(survey.updated_at, locale)}`
              : runningDescription(progress, locale);
        return (
          <article
            className={`${styles.surveyActiveRow} ${isRunning ? styles.surveyActiveRowProgress : ""}`}
            key={survey.id}
          >
            <div className={styles.surveyRowPrimary}>
              <div className={styles.surveyIdentity}>
                <h2>{survey.title}</h2>
                <p>{metadata}</p>
              </div>
              <div className={styles.surveyRowActions}>
                {isDraft ? (
                  <Link to={surveyDraftPath(survey.id)}>
                    {survey.latest_draft_revision ? "Review draft" : "View draft"} →
                  </Link>
                ) : (
                  <span className={styles.surveyStageLabel}>
                    {surveyStageLabel(progress.stage).toUpperCase()}
                  </span>
                )}
                {isRunning && <strong>{progress.percent}%</strong>}
                <button type="button" onClick={() => onCancel(survey)}>
                  Cancel
                </button>
              </div>
            </div>
            {isRunning && (
              <div
                className={styles.surveyProgress}
                role="progressbar"
                aria-label={`${surveyStageLabel(progress.stage)} progress`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={progress.percent}
              >
                <span style={{ width: `${progress.percent}%` }} />
              </div>
            )}
          </article>
        );
      })}
    </div>
  );
}

function ReportPreview({ title }: { title: string }) {
  return (
    <div className={styles.surveyReportThumbnail} aria-hidden="true">
      <div className={styles.surveyReportPaper}>
        <span />
        <small>SCHOLIGHT SURVEY</small>
        <strong>{title}</strong>
        <i />
        <i />
      </div>
    </div>
  );
}

export function CompletedSurveyList({
  items,
  locale,
  onDelete,
}: {
  items: SurveySummary[];
  locale: AppLocale;
  onDelete: (survey: SurveySummary) => void;
}) {
  if (!items.length) {
    return (
      <div className={styles.surveyEmpty}>
        <h2>No completed reports yet</h2>
        <p>Finished surveys will return here as Markdown reports.</p>
      </div>
    );
  }
  const reports = items.filter((item) => item.report_available);
  const withoutReports = items.filter((item) => !item.report_available);
  return (
    <div className={styles.surveyCompletedContent}>
      <div className={styles.surveyCompletedHeading}>
        <div>
          <h2>Completed reports</h2>
          <p>Open a finished survey as a research report.</p>
        </div>
        <span>Newest first ↓</span>
      </div>
      {reports.length > 0 && (
        <div className={styles.surveyReportGrid}>
          {reports.map((survey) => (
            <Link
              className={styles.surveyReportCard}
              to={surveyReportPath(survey.id)}
              key={survey.id}
            >
              <ReportPreview title={survey.title} />
              <div className={styles.surveyReportCardBody}>
                <h3>{survey.title}</h3>
                <div>
                  <span>
                    {survey.finished_at
                      ? formatReportDate(survey.finished_at, locale)
                      : "Completed"}
                  </span>
                  <span>{formatDurationBetween(survey.started_at, survey.finished_at)}</span>
                </div>
                <strong>Open report →</strong>
              </div>
            </Link>
          ))}
        </div>
      )}
      {withoutReports.length > 0 && (
        <section className={styles.surveyTerminalList} aria-labelledby="surveys-without-reports">
          <h2 id="surveys-without-reports">Surveys without reports</h2>
          {withoutReports.map((survey) => (
            <article key={survey.id}>
              <div>
                <h3>{survey.title}</h3>
                <p>
                  {survey.status === "cancelled" ? "Cancelled" : "Research did not complete"} ·{" "}
                  {formatRelativeTime(survey.updated_at, locale)}
                </p>
              </div>
              <button type="button" onClick={() => onDelete(survey)}>
                Delete
              </button>
            </article>
          ))}
        </section>
      )}
    </div>
  );
}
