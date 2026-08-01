import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { surveyApi } from "../../api/domain";
import { ApiError } from "../../api/errors";
import type { SurveySummary, SurveyView } from "../../api/types";
import { queryKeys } from "../../app/queryKeys";
import { surveyDraftPath } from "../../app/routes";
import { useAuth } from "../../auth/context";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { PageRefreshButton } from "../../components/PageRefreshButton";
import { SkeletonPulse } from "../../components/EditorialSkeleton";
import { useI18n } from "../../i18n/I18nProvider";
import { styles } from "../../styles/classes";
import { SurveyCancelDialog, SurveyLimitDialog, SurveySignInDialog } from "./SurveyDialogs";
import { ActiveSurveyList, CompletedSurveyList } from "./SurveyHubLists";
import { SURVEY_POLL_INTERVAL, shouldPollSummaries } from "./survey";

export function SurveyHubPage() {
  const { status } = useAuth();
  const { locale, messages } = useI18n();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const view: SurveyView = params.get("view") === "completed" ? "completed" : "active";
  const [request, setRequest] = useState("");
  const [requestError, setRequestError] = useState("");
  const requestId = useRef<string | undefined>(undefined);
  const [signInOpen, setSignInOpen] = useState(false);
  const [limitOpen, setLimitOpen] = useState(false);
  const [cancelTarget, setCancelTarget] = useState<SurveySummary | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<SurveySummary | null>(null);

  useEffect(() => {
    document.title = messages.titles.survey;
  }, [messages]);

  const list = useInfiniteQuery({
    queryKey: queryKeys.surveys(view),
    queryFn: ({ pageParam }) => surveyApi.list(view, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: status === "authenticated",
    refetchInterval: (query) => {
      if (view !== "active") return false;
      const data = query.state.data;
      return data?.pages.some((page) => shouldPollSummaries(page.items))
        ? SURVEY_POLL_INTERVAL
        : false;
    },
    refetchIntervalInBackground: false,
  });
  const items = list.data?.pages.flatMap((page) => page.items) ?? [];
  const quota = list.data?.pages[0]?.quota;

  const createSurvey = useMutation({
    mutationFn: () => {
      requestId.current ??= crypto.randomUUID();
      return surveyApi.create({
        initial_request: request.trim(),
        client_request_id: requestId.current,
      });
    },
    onSuccess: (survey) => {
      requestId.current = undefined;
      setRequest("");
      void queryClient.invalidateQueries({ queryKey: queryKeys.surveyRoot });
      navigate(surveyDraftPath(survey.id));
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === "survey_quota_exceeded") setLimitOpen(true);
      if (!(error instanceof ApiError) || !error.retryable) requestId.current = undefined;
    },
  });
  const cancelSurvey = useMutation({
    mutationFn: (surveyId: string) => surveyApi.cancel(surveyId),
    onSuccess: () => {
      setCancelTarget(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.surveyRoot });
    },
  });
  const deleteSurvey = useMutation({
    mutationFn: (surveyId: string) => surveyApi.remove(surveyId),
    onSuccess: () => {
      setDeleteTarget(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.surveyRoot });
    },
  });

  const requireSignIn = () => {
    if (status !== "authenticated") setSignInOpen(true);
  };
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (status !== "authenticated") return requireSignIn();
    if (!request.trim()) {
      setRequestError("Describe the research survey you want to run.");
      return;
    }
    setRequestError("");
    if (!createSurvey.isPending) createSurvey.mutate();
  };
  const setView = (next: SurveyView) => {
    setParams(next === "completed" ? { view: "completed" } : {});
  };
  const refresh = () => list.refetch();

  return (
    <main className={styles.surveyPage}>
      <header className={`${styles.surveyPageHeading} ${styles.pageHeadingAction}`}>
        <div>
          <span className={styles.eyebrow}>SURVEY</span>
          <h1>Research surveys</h1>
          <p>Start a survey, refine its research brief, and return to completed reports.</p>
        </div>
        {status === "authenticated" && (
          <PageRefreshButton label="surveys" refreshing={list.isFetching} onRefresh={refresh} />
        )}
      </header>
      <form className={styles.surveyStartForm} onSubmit={submit}>
        <label className={styles["sr-only"]} htmlFor="survey-request">
          Describe the survey you want to start
        </label>
        <input
          id="survey-request"
          type="text"
          value={request}
          readOnly={status !== "authenticated"}
          placeholder="Describe the survey you want to start…"
          aria-describedby={requestError ? "survey-request-error" : undefined}
          onFocus={requireSignIn}
          onClick={requireSignIn}
          onChange={(event) => {
            requestId.current = undefined;
            setRequestError("");
            setRequest(event.target.value);
          }}
        />
        <button className={styles.primaryButton} type="submit" disabled={createSurvey.isPending}>
          {createSurvey.isPending ? "Starting…" : "Start survey"}
        </button>
      </form>
      {requestError && (
        <p className={styles.surveyInlineError} id="survey-request-error" role="alert">
          {requestError}
        </p>
      )}
      {createSurvey.error &&
        !(
          createSurvey.error instanceof ApiError &&
          createSurvey.error.code === "survey_quota_exceeded"
        ) && (
          <p className={styles.surveyInlineError} role="alert">
            {createSurvey.error instanceof ApiError
              ? createSurvey.error.message
              : "Unable to start this survey."}
          </p>
        )}
      <div className={styles.surveyTabs}>
        <div role="tablist" aria-label="Survey views">
          <button
            type="button"
            role="tab"
            aria-selected={view === "active"}
            onClick={() => setView("active")}
          >
            Running
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === "completed"}
            onClick={() => setView("completed")}
          >
            Completed
          </button>
        </div>
      </div>

      {status !== "authenticated" ? (
        <div className={styles.surveySignInEmpty}>
          <h2>Sign in to view your surveys</h2>
          <p>Running surveys, draft revisions, and completed reports are saved to your account.</p>
          <button type="button" onClick={() => setSignInOpen(true)}>
            Sign in
          </button>
        </div>
      ) : list.isPending ? (
        <SkeletonPulse label="Loading research surveys" className={styles.surveySkeleton}>
          <span />
          <span />
          <span />
        </SkeletonPulse>
      ) : list.error ? (
        <div className={styles.surveyErrorState} role="alert">
          <h2>Surveys are unavailable</h2>
          <p>{list.error instanceof ApiError ? list.error.message : "Unable to load surveys."}</p>
          <button type="button" onClick={() => void list.refetch()}>
            Retry
          </button>
        </div>
      ) : view === "active" ? (
        <ActiveSurveyList items={items} locale={locale} onCancel={setCancelTarget} />
      ) : (
        <CompletedSurveyList items={items} locale={locale} onDelete={setDeleteTarget} />
      )}
      {status === "authenticated" && list.hasNextPage && (
        <div className={styles.surveyLoadMore}>
          <button
            type="button"
            disabled={list.isFetchingNextPage}
            onClick={() => void list.fetchNextPage()}
          >
            {list.isFetchingNextPage ? "Loading…" : "Load more"}
          </button>
        </div>
      )}

      <SurveySignInDialog open={signInOpen} onOpenChange={setSignInOpen} />
      <SurveyLimitDialog
        open={limitOpen}
        quota={quota}
        onOpenChange={setLimitOpen}
        onReview={() => {
          setLimitOpen(false);
          setView("active");
        }}
      />
      <SurveyCancelDialog
        open={Boolean(cancelTarget)}
        busy={cancelSurvey.isPending}
        error={cancelSurvey.error instanceof Error ? cancelSurvey.error.message : undefined}
        onOpenChange={(open) => {
          if (!open) setCancelTarget(null);
        }}
        onConfirm={() => {
          if (cancelTarget) cancelSurvey.mutate(cancelTarget.id);
        }}
      />
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="Delete this survey?"
        description="This permanently removes the survey record. This action cannot be undone."
        busy={deleteSurvey.isPending}
        error={deleteSurvey.error instanceof Error ? deleteSurvey.error.message : undefined}
        confirmLabel="Delete"
        busyLabel="Deleting…"
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        onConfirm={() => {
          if (deleteTarget) deleteSurvey.mutate(deleteTarget.id);
        }}
      />
    </main>
  );
}
