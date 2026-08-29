import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { surveyApi } from "../../api/domain";
import { ApiError } from "../../api/errors";
import { queryKeys } from "../../app/queryKeys";
import { routes, surveyDraftPath, withQuery } from "../../app/routes";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { SurveyDetailSkeleton } from "../../components/EditorialSkeleton";
import { formatDurationBetween, formatFullDateTime } from "../../i18n/format";
import { useI18n } from "../../i18n/I18nProvider";
import { styles } from "../../styles/classes";
import { SurveyMarkdown } from "./SurveyMarkdown";
import {
  archiveFilename,
  artifactUrlMap,
  hasOpeningFigure,
  pdfFilename,
  surveyTitle,
} from "./survey";

export function SurveyReportPage() {
  const surveyId = useParams().surveyId ?? "";
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { locale } = useI18n();
  const [deleteOpen, setDeleteOpen] = useState(false);
  const replacementId = useRef<string | undefined>(undefined);
  const survey = useQuery({
    queryKey: queryKeys.survey(surveyId),
    queryFn: () => surveyApi.get(surveyId),
    enabled: Boolean(surveyId),
  });
  const report = useQuery({
    queryKey: queryKeys.surveyReport(surveyId),
    queryFn: () => surveyApi.report(surveyId),
    enabled: Boolean(surveyId),
  });
  const artifacts = useQuery({
    queryKey: queryKeys.surveyArtifacts(surveyId),
    queryFn: () => surveyApi.artifacts(surveyId),
    enabled: Boolean(surveyId),
    staleTime: 4 * 60_000,
  });
  const imageArtifacts = useMemo(
    () => artifactUrlMap(artifacts.data?.items ?? []),
    [artifacts.data?.items],
  );
  const openingFigureAvailable = hasOpeningFigure(artifacts.data?.items ?? []);
  const remove = useMutation({
    mutationFn: () => surveyApi.remove(surveyId),
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: queryKeys.survey(surveyId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.surveyRoot });
      navigate(withQuery(routes.survey.path, { view: "completed" }), { replace: true });
    },
  });
  const packageDownload = useMutation({
    mutationFn: () => surveyApi.downloadPackage(surveyId),
  });
  const pdfDownload = useMutation({
    mutationFn: () => surveyApi.downloadPdf(surveyId),
  });
  const runAgain = useMutation({
    mutationFn: () => {
      const initialRequest = survey.data?.initial_request.trim();
      if (!initialRequest) throw new Error("The original request is unavailable.");
      replacementId.current ??= crypto.randomUUID();
      return surveyApi.create({
        initial_request: initialRequest,
        client_request_id: replacementId.current,
      });
    },
    onSuccess: (replacement) => {
      replacementId.current = undefined;
      void queryClient.invalidateQueries({ queryKey: queryKeys.surveyRoot });
      navigate(surveyDraftPath(replacement.id));
    },
    onError: (error) => {
      if (!(error instanceof ApiError) || !error.retryable) replacementId.current = undefined;
    },
  });

  useEffect(() => {
    if (survey.data)
      document.title = `${surveyTitle(survey.data.title, survey.data.initial_request)} — Scholight`;
  }, [survey.data]);

  if (survey.isPending || report.isPending) return <SurveyDetailSkeleton />;
  if (!survey.data || survey.error || report.error || !report.data) {
    const error = survey.error ?? report.error;
    const pending = error instanceof ApiError && error.code === "survey_archive_pending";
    return (
      <>
        <main className={styles.surveyPage}>
          <Link
            className={styles.surveyBackLink}
            to={withQuery(routes.survey.path, { view: "completed" })}
          >
            ← Back to completed reports
          </Link>
          <div className={styles.surveyErrorState} role="alert">
            <h1>{pending ? "Saving report" : "Unable to open this report"}</h1>
            <p>
              {error instanceof ApiError ? error.message : "The report is temporarily unavailable."}
            </p>
            <div className={styles.surveyErrorActions}>
              <button
                type="button"
                onClick={() =>
                  void Promise.all([survey.refetch(), report.refetch(), artifacts.refetch()])
                }
              >
                Retry
              </button>
              {survey.data && (
                <button
                  className={styles.dangerButton}
                  type="button"
                  onClick={() => setDeleteOpen(true)}
                >
                  Delete
                </button>
              )}
            </div>
          </div>
        </main>
        <ConfirmDialog
          open={deleteOpen}
          title="Delete this report?"
          description="This permanently removes the survey report and its archived research artifacts. This action cannot be undone."
          busy={remove.isPending}
          error={remove.error instanceof Error ? remove.error.message : undefined}
          confirmLabel="Delete"
          busyLabel="Deleting…"
          onOpenChange={setDeleteOpen}
          onConfirm={() => remove.mutate()}
        />
      </>
    );
  }

  const title = surveyTitle(survey.data.title, survey.data.initial_request);
  const saveBlob = (blob: Blob, filename: string) => {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  };
  const download = () => {
    packageDownload.mutate(undefined, {
      onSuccess: (blob) => saveBlob(blob, archiveFilename(title)),
    });
  };
  const downloadPdf = () => {
    pdfDownload.mutate(undefined, {
      onSuccess: (blob) => saveBlob(blob, pdfFilename(title)),
    });
  };

  return (
    <main className={`${styles.surveyPage} ${styles.surveyReportPage}`}>
      <Link
        className={styles.surveyBackLink}
        to={withQuery(routes.survey.path, { view: "completed" })}
      >
        ← Back to completed reports
      </Link>
      <div className={styles.surveyReportHeader}>
        <h1>{title}</h1>
        <div>
          <button
            className={styles.secondaryButton}
            type="button"
            disabled={runAgain.isPending}
            onClick={() => runAgain.mutate()}
          >
            {runAgain.isPending ? "Preparing…" : "Run again"}
          </button>
          <button
            className={styles.secondaryButton}
            type="button"
            disabled={pdfDownload.isPending}
            onClick={downloadPdf}
          >
            {pdfDownload.isPending ? "Preparing…" : "Download PDF"}
          </button>

          <button
            className={styles.secondaryButton}
            type="button"
            disabled={packageDownload.isPending}
            onClick={download}
          >
            {packageDownload.isPending ? "Preparing…" : "Download ZIP"}
          </button>
          <button className={styles.dangerButton} type="button" onClick={() => setDeleteOpen(true)}>
            Delete
          </button>
        </div>
      </div>
      {survey.data.error_code === "survey_quality_degraded" && (
        <div className={styles.surveyQualityNotice} role="status">
          <strong>Report delivered with quality notes</strong>
          <p>
            Some research quality checks were incomplete. You can still read and download the
            report, and this run was not counted against your Survey allowance.
          </p>
        </div>
      )}
      <div className={styles.surveyReportLayout}>
        <article className={styles.surveyReportDocument}>
          <SurveyMarkdown markdown={report.data} imageArtifacts={imageArtifacts} />
        </article>
        <aside className={styles.surveyReportDetails}>
          <span>REPORT DETAILS</span>
          <dl>
            <div>
              <dt>Status</dt>
              <dd>Completed</dd>
            </div>
            <div>
              <dt>Finished</dt>
              <dd>
                {survey.data.finished_at
                  ? formatFullDateTime(survey.data.finished_at, locale)
                  : "—"}
              </dd>
            </div>
            <div>
              <dt>Research time</dt>
              <dd>{formatDurationBetween(survey.data.started_at, survey.data.finished_at)}</dd>
            </div>
            <div>
              <dt>Opening figure</dt>
              <dd>
                {artifacts.isPending
                  ? "Checking…"
                  : openingFigureAvailable
                    ? "Available"
                    : "Unavailable"}
              </dd>
            </div>
            <div>
              <dt>Format</dt>
              <dd>
                {openingFigureAvailable
                  ? "PDF · Markdown + images (.zip)"
                  : "PDF · Markdown (.zip)"}
              </dd>
            </div>
          </dl>
          {packageDownload.error && (
            <p className={styles.surveyInlineError} role="alert">
              {packageDownload.error instanceof Error
                ? packageDownload.error.message
                : "The report package is temporarily unavailable."}
            </p>
          )}
          {pdfDownload.error && (
            <p className={styles.surveyInlineError} role="alert">
              {pdfDownload.error instanceof Error
                ? pdfDownload.error.message
                : "The report PDF is temporarily unavailable."}
            </p>
          )}
          {runAgain.error && (
            <p className={styles.surveyInlineError} role="alert">
              {runAgain.error instanceof Error
                ? runAgain.error.message
                : "Unable to prepare a new survey."}
            </p>
          )}
          {artifacts.error && <p>Embedded images are temporarily unavailable.</p>}
        </aside>
      </div>
      <ConfirmDialog
        open={deleteOpen}
        title="Delete this report?"
        description="This permanently removes the survey report and its archived research artifacts. This action cannot be undone."
        busy={remove.isPending}
        error={remove.error instanceof Error ? remove.error.message : undefined}
        confirmLabel="Delete"
        busyLabel="Deleting…"
        onOpenChange={setDeleteOpen}
        onConfirm={() => remove.mutate()}
      />
    </main>
  );
}
