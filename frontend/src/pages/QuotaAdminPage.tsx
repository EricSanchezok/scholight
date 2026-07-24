import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { adminApi } from "../api/domain";
import { ApiError } from "../api/errors";
import type { AdminAuditEvent, AdminUserLookup, QuotaOverrideRequest } from "../api/types";
import { queryKeys } from "../app/queryKeys";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EditorialRowsSkeleton } from "../components/EditorialSkeleton";
import { formatFullDateTime } from "../i18n/format";
import { useI18n } from "../i18n/I18nProvider";
import { styles } from "../styles/classes";

const MAX_LIMIT = 1_000_000;

function optionalLimit(value: string): number | null {
  return value.trim() === "" ? null : Number(value);
}

function limitLabel(value: number | null): string {
  return value === null ? "Deployment default" : value.toLocaleString("en-US");
}

function changeSummary(current: AdminUserLookup, next: QuotaOverrideRequest): string {
  return `Standard: ${limitLabel(current.quotas.standard.override_limit)} → ${limitLabel(
    next.standard,
  )}. Thorough: ${limitLabel(current.quotas.thorough.override_limit)} → ${limitLabel(
    next.thorough,
  )}.`;
}

function auditValue(value: unknown): string {
  return typeof value === "number" ? value.toLocaleString("en-US") : "Deployment default";
}

function AuditChange({ event }: { event: AdminAuditEvent }) {
  if (event.action === "quota_overrides_updated") {
    return (
      <span>
        Standard {auditValue(event.before_state.standard)} →{" "}
        {auditValue(event.after_state.standard)}; Thorough {auditValue(event.before_state.thorough)}{" "}
        → {auditValue(event.after_state.thorough)}
      </span>
    );
  }
  return (
    <span>
      {event.action === "admin_granted" ? "Administrator granted" : "Administrator revoked"}
    </span>
  );
}

export function QuotaAdminPage() {
  const { locale, messages } = useI18n();
  const [email, setEmail] = useState("");
  const [standard, setStandard] = useState("");
  const [thorough, setThorough] = useState("");
  const [pending, setPending] = useState<QuotaOverrideRequest | null>(null);
  const [feedback, setFeedback] = useState("");
  const audit = useQuery({
    queryKey: queryKeys.adminAudit,
    queryFn: () => adminApi.auditEvents(20),
  });
  const lookup = useMutation({
    mutationFn: (targetEmail: string) => adminApi.lookupUser(targetEmail),
    onSuccess: (data) => {
      setStandard(data.quotas.standard.override_limit?.toString() ?? "");
      setThorough(data.quotas.thorough.override_limit?.toString() ?? "");
      setFeedback("");
    },
  });
  const update = useMutation({
    mutationFn: (body: QuotaOverrideRequest) => {
      if (!lookup.data) throw new Error("No quota target");
      return adminApi.updateQuotaOverrides(lookup.data.user.id, body);
    },
    onSuccess: async (_result, body) => {
      const targetEmail = lookup.data?.user.email;
      setPending(null);
      if (targetEmail) await lookup.mutateAsync(targetEmail);
      await audit.refetch();
      setStandard(body.standard?.toString() ?? "");
      setThorough(body.thorough?.toString() ?? "");
      setFeedback("Quota settings saved.");
    },
  });

  useEffect(() => {
    document.title = messages.titles.quotaAdmin;
  }, [messages.titles.quotaAdmin]);

  const lookupError =
    lookup.error instanceof ApiError ? lookup.error.message : "The user could not be found.";
  const updateError =
    update.error instanceof ApiError
      ? update.error.message
      : update.error
        ? "Quota settings could not be saved."
        : undefined;
  const valuesValid = [standard, thorough].every((value) => {
    const parsed = optionalLimit(value);
    return parsed === null || (Number.isInteger(parsed) && parsed >= 0 && parsed <= MAX_LIMIT);
  });
  const proposed: QuotaOverrideRequest = {
    standard: optionalLimit(standard),
    thorough: optionalLimit(thorough),
  };
  const beginSave = (body: QuotaOverrideRequest) => {
    setFeedback("");
    update.reset();
    setPending(body);
  };

  return (
    <main className={`${styles.ledgerPage} ${styles.adminPage}`}>
      <header className={styles.ledgerHeading}>
        <span className={styles.eyebrow}>Administration</span>
        <h1>Quota administration</h1>
        <p>Adjust one known user’s daily Scholight allowance and keep every change auditable.</p>
      </header>

      <section className={styles.adminLookup} aria-labelledby="find-quota-user">
        <div>
          <h2 id="find-quota-user">Find a user</h2>
          <p>Enter the complete email address. Partial searches are not available.</p>
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            setFeedback("");
            void lookup.mutateAsync(email.trim());
          }}
        >
          <label>
            <span>User email</span>
            <input
              type="email"
              required
              value={email}
              autoComplete="off"
              placeholder="user@example.com"
              onChange={(event) => setEmail(event.target.value)}
            />
          </label>
          <button className={styles.primaryButton} disabled={lookup.isPending}>
            {lookup.isPending ? "Finding…" : "Find user"}
          </button>
        </form>
      </section>

      {lookup.error && (
        <div className={styles.sectionError} role="alert">
          <span>{lookupError}</span>
          <button type="button" onClick={() => void lookup.mutateAsync(email.trim())}>
            Retry
          </button>
        </div>
      )}

      {lookup.data ? (
        <section className={styles.adminTarget} aria-labelledby="quota-target">
          <div className={styles.adminTargetIdentity}>
            <div>
              <span className={styles.fieldCaption}>QUOTA TARGET</span>
              <h2 id="quota-target">{lookup.data.user.display_name || lookup.data.user.email}</h2>
              <p>{lookup.data.user.email}</p>
            </div>
            <span>{lookup.data.user.account_status}</span>
          </div>

          <div className={styles.adminQuotaHeader} aria-hidden="true">
            <span>Strength</span>
            <span>Used today</span>
            <span>Default</span>
            <span>Effective</span>
            <span>Custom daily limit</span>
          </div>
          <div className={styles.adminQuotaRows}>
            {(["standard", "thorough"] as const).map((strength) => {
              const quota = lookup.data.quotas[strength];
              const value = strength === "standard" ? standard : thorough;
              const setter = strength === "standard" ? setStandard : setThorough;
              return (
                <div className={styles.adminQuotaRow} key={strength}>
                  <strong>{strength === "standard" ? "Standard" : "Thorough"}</strong>
                  <span data-label="Used today">{quota.used.toLocaleString(locale)}</span>
                  <span data-label="Default">{quota.default_limit.toLocaleString(locale)}</span>
                  <span data-label="Effective">{quota.effective_limit.toLocaleString(locale)}</span>
                  <label>
                    <span className="sr-only">
                      {strength === "standard" ? "Standard" : "Thorough"} custom daily limit
                    </span>
                    <input
                      type="number"
                      min={0}
                      max={MAX_LIMIT}
                      step={1}
                      inputMode="numeric"
                      value={value}
                      placeholder={quota.default_limit.toString()}
                      onChange={(event) => setter(event.target.value)}
                    />
                  </label>
                </div>
              );
            })}
          </div>
          <p className={styles.adminQuotaHint}>
            Leave a field empty to use the deployment default. A value of 0 disables that search
            strength. Today’s existing usage is not reset.
          </p>
          {!valuesValid && (
            <p className={styles.formMessageError} role="alert">
              Use a whole number from 0 to 1,000,000, or leave the field empty.
            </p>
          )}
          {feedback && (
            <p className={styles.formMessageSuccess} role="status">
              {feedback}
            </p>
          )}
          <div className={styles.adminQuotaActions}>
            <button
              className={styles.secondaryButton}
              type="button"
              onClick={() => beginSave({ standard: null, thorough: null })}
            >
              Restore defaults
            </button>
            <button
              className={styles.primaryButton}
              type="button"
              disabled={!valuesValid}
              onClick={() => beginSave(proposed)}
            >
              Save changes
            </button>
          </div>
        </section>
      ) : (
        !lookup.isPending && (
          <section className={styles.adminEmpty} aria-label="No quota target selected">
            <p>Find an account to review its current allowance and today’s usage.</p>
          </section>
        )
      )}

      <section className={styles.adminAudit} aria-labelledby="recent-admin-events">
        <div className={styles.recentUsageHeading}>
          <h2 id="recent-admin-events">Recent administration</h2>
          <button type="button" onClick={() => void audit.refetch()} disabled={audit.isFetching}>
            {audit.isFetching ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        {audit.error && !audit.data ? (
          <div className={styles.sectionError} role="alert">
            <span>Administration history is temporarily unavailable.</span>
            <button type="button" onClick={() => void audit.refetch()}>
              Retry
            </button>
          </div>
        ) : audit.isPending ? (
          <EditorialRowsSkeleton label="Loading administration history" rows={3} />
        ) : audit.data?.length ? (
          <div className={styles.adminAuditLedger}>
            {audit.data.map((event) => (
              <div className={styles.adminAuditRow} key={event.event_id}>
                <time dateTime={event.created_at}>
                  {formatFullDateTime(event.created_at, locale)}
                </time>
                <div>
                  <strong>{event.target_email}</strong>
                  <AuditChange event={event} />
                </div>
                <span>by {event.actor_identifier}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className={styles.ledgerEmpty}>
            <h3>No administration events</h3>
            <p>Saved quota changes will appear here.</p>
          </div>
        )}
      </section>

      <ConfirmDialog
        open={pending !== null}
        title={
          pending?.standard === null && pending?.thorough === null
            ? "Restore defaults?"
            : "Save quota changes?"
        }
        description={
          pending && lookup.data
            ? `${lookup.data.user.email}. ${changeSummary(lookup.data, pending)}`
            : ""
        }
        confirmLabel={
          pending?.standard === null && pending?.thorough === null
            ? "Restore defaults"
            : "Confirm changes"
        }
        busyLabel="Saving…"
        busy={update.isPending}
        error={updateError}
        tone="primary"
        onOpenChange={(open) => {
          if (!open && !update.isPending) setPending(null);
        }}
        onConfirm={() => {
          if (pending) void update.mutateAsync(pending);
        }}
      />
    </main>
  );
}
