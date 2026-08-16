import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { surveyApi } from "../../api/domain";
import { ApiError } from "../../api/errors";
import { queryKeys } from "../../app/queryKeys";
import { contentSwapMotion } from "../../app/motion";
import { routes, surveyDraftPath } from "../../app/routes";
import { SurveyDetailSkeleton } from "../../components/EditorialSkeleton";
import { formatRelativeTime } from "../../i18n/format";
import { useI18n } from "../../i18n/I18nProvider";
import { styles } from "../../styles/classes";
import { SurveyStartDialog } from "./SurveyDialogs";
import { SurveyDraftHistory } from "./SurveyDraftHistory";
import { SurveyDraftLoading } from "./SurveyDraftLoading";
import { SurveyMarkdown } from "./SurveyMarkdown";
import { SurveyReuseSection } from "./SurveyReuseSection";
import { mutationMessage, queueAhead, SURVEY_POLL_INTERVAL, surveyTitle } from "./survey";

export function SurveyDraftPage() {
  const surveyId = useParams().surveyId ?? "";
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { locale } = useI18n();
  const [selectedId, setSelectedId] = useState<string>();
  const [editing, setEditing] = useState(false);
  const [source, setSource] = useState("");
  const [feedback, setFeedback] = useState("");
  const [startOpen, setStartOpen] = useState(false);
  const [notifyOnCompletion, setNotifyOnCompletion] = useState(true);
  const revisionId = useRef<string | undefined>(undefined);
  const manualId = useRef<string | undefined>(undefined);
  const replacementId = useRef<string | undefined>(undefined);
  const startRequest = useRef<{ clientRequestId: string; notifyOnCompletion: boolean } | undefined>(
    undefined,
  );

  const survey = useQuery({
    queryKey: queryKeys.survey(surveyId),
    queryFn: () => surveyApi.get(surveyId),
    enabled: Boolean(surveyId),
  });
  const drafts = useQuery({
    queryKey: queryKeys.surveyDrafts(surveyId),
    queryFn: () => surveyApi.drafts(surveyId),
    enabled: Boolean(surveyId),
    refetchInterval: (query) => {
      const values = query.state.data ?? [];
      return values.some((draft) => draft.status === "queued" || draft.status === "running")
        ? SURVEY_POLL_INTERVAL
        : false;
    },
    refetchIntervalInBackground: false,
  });
  const progress = useQuery({
    queryKey: queryKeys.surveyProgress(surveyId),
    queryFn: () => surveyApi.progress(surveyId),
    enabled: Boolean(surveyId),
    refetchInterval: (query) => {
      const stage = query.state.data?.stage;
      return stage === "waiting_for_draft" || stage === "drafting" ? SURVEY_POLL_INTERVAL : false;
    },
    refetchIntervalInBackground: false,
  });

  const values = useMemo(() => drafts.data ?? [], [drafts.data]);
  const readyDrafts = useMemo(
    () => values.filter((draft) => draft.status === "ready" && draft.markdown),
    [values],
  );
  const current = [...readyDrafts].sort((a, b) => (b.revision ?? 0) - (a.revision ?? 0))[0];
  const selected = readyDrafts.find((draft) => draft.id === selectedId) ?? current;
  const latest = [...values].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))[0];
  const active = latest?.status === "queued" || latest?.status === "running" ? latest : undefined;
  const failed = latest?.status === "failed" ? latest : undefined;
  const viewingHistory = Boolean(selected && current && selected.id !== current.id);
  const atLimit = (current?.revision ?? 0) >= 10;

  useEffect(() => {
    if (survey.data)
      document.title = `${surveyTitle(survey.data.title, survey.data.initial_request)} — Scholight`;
  }, [survey.data]);

  useEffect(() => {
    setNotifyOnCompletion(true);
    startRequest.current = undefined;
    replacementId.current = undefined;
  }, [surveyId]);

  const refresh = async () => {
    await Promise.all([survey.refetch(), drafts.refetch(), progress.refetch()]);
  };
  const revise = useMutation({
    mutationFn: (message: string) => {
      revisionId.current ??= crypto.randomUUID();
      return surveyApi.reviseDraft(surveyId, { message, client_request_id: revisionId.current });
    },
    onSuccess: async () => {
      revisionId.current = undefined;
      setFeedback("");
      setSelectedId(undefined);
      await refresh();
    },
    onError: (error) => {
      if (!(error instanceof ApiError) || !error.retryable) revisionId.current = undefined;
    },
  });
  const save = useMutation({
    mutationFn: () => {
      manualId.current ??= crypto.randomUUID();
      return surveyApi.saveManualDraft(surveyId, {
        markdown: source,
        message: "Manual draft revision",
        client_request_id: manualId.current,
      });
    },
    onSuccess: async () => {
      manualId.current = undefined;
      setEditing(false);
      setSelectedId(undefined);
      await refresh();
    },
    onError: (error) => {
      if (!(error instanceof ApiError) || !error.retryable) manualId.current = undefined;
    },
  });
  const start = useMutation({
    mutationFn: () => {
      startRequest.current ??= {
        clientRequestId: crypto.randomUUID(),
        notifyOnCompletion,
      };
      return surveyApi.start(surveyId, {
        client_request_id: startRequest.current.clientRequestId,
        notify_on_completion: startRequest.current.notifyOnCompletion,
      });
    },
    onSuccess: () => {
      startRequest.current = undefined;
      void queryClient.invalidateQueries({ queryKey: queryKeys.surveyRoot });
      navigate(routes.survey.path);
    },
    onError: (error) => {
      if (!(error instanceof ApiError) || !error.retryable) startRequest.current = undefined;
    },
  });
  const createReplacement = useMutation({
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

  if (survey.isPending || drafts.isPending || progress.isPending) return <SurveyDetailSkeleton />;
  if (!survey.data || survey.error || drafts.error || progress.error) {
    const error = survey.error ?? drafts.error ?? progress.error;
    return (
      <main className={styles.surveyPage}>
        <div className={styles.surveyErrorState} role="alert">
          <h1>Unable to open this survey</h1>
          <p>{mutationMessage(error, "The survey is temporarily unavailable.")}</p>
          <button type="button" onClick={() => void refresh()}>
            Retry
          </button>
        </div>
      </main>
    );
  }

  const canEdit = survey.data.status === "drafting";
  const canReuse = survey.data.status === "failed" || survey.data.status === "cancelled";

  const statusLine = canReuse
    ? `${survey.data.status === "cancelled" ? "Survey cancelled" : "Survey failed"}  ·  Draft and original request preserved`
    : active?.status === "queued"
      ? `Waiting to refine  ·  ${queueAhead(progress.data)} requests ahead  ·  ${progress.data.queue ? `Queued ${formatRelativeTime(progress.data.queue.queued_at, locale)}` : "Queued"}`
      : active
        ? "Generating draft"
        : failed
          ? "Draft generation failed"
          : current
            ? `Draft ready  ·  Draft ${current.revision} of 10  ·  ${10 - (current.revision ?? 0)} revisions remaining`
            : "Preparing research brief";

  return (
    <main className={styles.surveyPage}>
      <Link className={styles.surveyBackLink} to={routes.survey.path}>
        ← Back to surveys
      </Link>
      <header className={styles.surveyDraftHeader}>
        <h1>{surveyTitle(survey.data.title, survey.data.initial_request)}</h1>
        <p>{statusLine}</p>
      </header>
      <div className={styles.surveyDraftLayout}>
        <div className={styles.surveyDraftMain}>
          <div className={styles.surveyDraftSectionHeading}>
            <h2>{viewingHistory ? `Draft v${selected?.revision}` : "Current research brief"}</h2>
            <span>{editing ? "MARKDOWN SOURCE" : "RENDERED PREVIEW"}</span>
          </div>

          <div className={styles.surveyDraftStage}>
            <AnimatePresence initial={false} mode="wait">
              {active ? (
                <m.div key={`active-${active.id}`} {...contentSwapMotion}>
                  <SurveyDraftLoading status={active.status === "queued" ? "queued" : "running"} />
                </m.div>
              ) : failed && (!current || failed.created_at > current.created_at) ? (
                <m.div key={`failed-${failed.id}`} {...contentSwapMotion}>
                  <div className={styles.surveyDraftFailure} role="alert">
                    <h3>Draft generation failed</h3>
                    <p>{failed.error_message ?? "The draft could not be prepared."}</p>
                    <button
                      type="button"
                      disabled={revise.isPending}
                      onClick={() => revise.mutate(failed.user_message)}
                    >
                      Retry draft
                    </button>
                  </div>
                </m.div>
              ) : selected?.markdown ? (
                <m.div
                  key={`${selected.id}-${editing ? "source" : "preview"}`}
                  {...contentSwapMotion}
                >
                  {editing ? (
                    <textarea
                      className={styles.surveyDraftEditor}
                      value={source}
                      onChange={(event) => setSource(event.target.value)}
                      aria-label="Markdown source"
                    />
                  ) : (
                    <div className={styles.surveyDraftPreview}>
                      <SurveyMarkdown markdown={selected.markdown} compact />
                    </div>
                  )}
                  {viewingHistory ? (
                    <div className={styles.surveyDraftActions}>
                      <button
                        type="button"
                        className={styles.secondaryButton}
                        onClick={() => setSelectedId(undefined)}
                      >
                        Return to current
                      </button>
                    </div>
                  ) : canEdit && editing ? (
                    <div className={styles.surveyDraftActions}>
                      <button
                        type="button"
                        className={styles.secondaryButton}
                        onClick={() => {
                          setEditing(false);
                          setSource(current?.markdown ?? "");
                        }}
                      >
                        Cancel
                      </button>
                      <button
                        type="button"
                        className={styles.primaryButton}
                        disabled={!source.trim() || save.isPending}
                        onClick={() => save.mutate()}
                      >
                        {save.isPending ? "Saving…" : "Save"}
                      </button>
                    </div>
                  ) : canEdit ? (
                    <div className={styles.surveyDraftActions}>
                      <button
                        type="button"
                        className={styles.secondaryButton}
                        onClick={() => {
                          setSource(current?.markdown ?? "");
                          setEditing(true);
                        }}
                      >
                        Edit draft
                      </button>
                      <button
                        type="button"
                        className={styles.primaryButton}
                        onClick={() => setStartOpen(true)}
                      >
                        Approve &amp; start
                      </button>
                    </div>
                  ) : null}
                  {(save.error || revise.error) && (
                    <p className={styles.surveyInlineError} role="alert">
                      {mutationMessage(save.error ?? revise.error, "Unable to save this revision.")}
                    </p>
                  )}
                </m.div>
              ) : (
                <m.div key="empty" {...contentSwapMotion}>
                  <div className={styles.surveyDraftFailure}>
                    <p>No draft is available yet.</p>
                  </div>
                </m.div>
              )}
            </AnimatePresence>
          </div>

          {canReuse && !viewingHistory && (
            <SurveyReuseSection
              busy={createReplacement.isPending}
              error={
                createReplacement.error
                  ? mutationMessage(
                      createReplacement.error,
                      "Unable to prepare a new survey from this request.",
                    )
                  : undefined
              }
              onReuse={() => createReplacement.mutate()}
            />
          )}
          {canEdit && !active && current && !editing && !viewingHistory && !atLimit && (
            <section className={styles.surveyRefineSection}>
              <h2>Refine this draft</h2>
              <p>
                Describe what should change. A new revision will be generated from your feedback.
              </p>
              <textarea
                value={feedback}
                onChange={(event) => {
                  revisionId.current = undefined;
                  setFeedback(event.target.value);
                }}
                placeholder="Describe what is wrong or what should change in the next draft…"
                aria-label="Revision feedback"
              />
              <button
                type="button"
                disabled={!feedback.trim() || revise.isPending}
                onClick={() => revise.mutate(feedback.trim())}
              >
                {revise.isPending ? "Submitting…" : "Generate revision"}
              </button>
            </section>
          )}
          {canEdit && atLimit && (
            <p className={styles.surveyRevisionLimit}>
              This draft has reached v10. You can still edit it or approve the survey.
            </p>
          )}
        </div>
        <SurveyDraftHistory
          drafts={values}
          currentId={current?.id}
          selectedId={selectedId}
          initialRequest={survey.data.initial_request}
          readOnly={!canEdit}
          locale={locale}
          onSelect={(id) => {
            setEditing(false);
            setSelectedId(id);
          }}
        />
      </div>
      <SurveyStartDialog
        open={startOpen}
        busy={start.isPending}
        notifyOnCompletion={notifyOnCompletion}
        error={
          start.error ? mutationMessage(start.error, "Unable to start this survey.") : undefined
        }
        onNotifyChange={(notify) => {
          startRequest.current = undefined;
          setNotifyOnCompletion(notify);
        }}
        onOpenChange={setStartOpen}
        onConfirm={() => start.mutate()}
      />
    </main>
  );
}
