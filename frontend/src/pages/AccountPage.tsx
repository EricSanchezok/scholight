import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as m from "motion/react-m";
import { useState } from "react";

import { accountApi } from "../api/domain";
import { ApiError } from "../api/errors";
import type { Session } from "../api/types";
import { ledgerRowMotion } from "../app/motion";
import { queryKeys } from "../app/queryKeys";
import { useAuth } from "../auth/context";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EditorialRowsSkeleton } from "../components/EditorialSkeleton";
import { ExternalLinkIcon } from "../components/icons";
import { PageRefreshButton } from "../components/PageRefreshButton";
import { SharedAvatar } from "../components/SharedAvatar";
import { productConfig } from "../config/product";
import { formatFullDateTime } from "../i18n/format";
import { useI18n, type AppLocale } from "../i18n/I18nProvider";
import type { Messages } from "../i18n/en";
import { parseUserAgent, sortSessions } from "../lib/account";
import { styles } from "../styles/classes";

function seenAt(
  value: string | null,
  current: boolean,
  locale: AppLocale,
  messages: Messages,
): string {
  if (current) return messages.account.activeNow;
  if (!value) return messages.account.lastSeenUnavailable;
  return messages.account.lastSeen(formatFullDateTime(value, locale));
}

export function AccountPage() {
  const { locale, messages } = useI18n();
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const [pendingSession, setPendingSession] = useState<Session | null>(null);
  const [revokeOthersOpen, setRevokeOthersOpen] = useState(false);
  const profile = useQuery({
    queryKey: queryKeys.profile,
    queryFn: accountApi.profile,
    initialData: user ?? undefined,
  });
  const sessions = useQuery({
    queryKey: queryKeys.sessions,
    queryFn: accountApi.sessions,
    staleTime: 30_000,
  });
  const revokeSession = useMutation({
    mutationFn: (sessionId: number) => accountApi.revokeSession(sessionId),
    onSuccess: async () => {
      setPendingSession(null);
      await queryClient.invalidateQueries({ queryKey: queryKeys.sessions });
    },
  });
  const revokeOthers = useMutation({
    mutationFn: accountApi.revokeOtherSessions,
    onSuccess: async () => {
      setRevokeOthersOpen(false);
      await queryClient.invalidateQueries({ queryKey: queryKeys.sessions });
    },
  });
  const orderedSessions = sortSessions(sessions.data ?? []);
  const otherSessions = orderedSessions.filter((session) => !session.current);
  const displayName =
    profile.data?.display_name?.trim() ||
    profile.data?.email.split("@")[0] ||
    "SanchezCloud account";
  const email = profile.data?.email ?? user?.email ?? "";

  return (
    <main className={styles.accountPage}>
      <header className={styles.accountIntro}>
        <h1>Account</h1>
        <span>View your SanchezCloud profile and active Scholight sessions.</span>
      </header>
      <section className={styles.accountSection}>
        <div className={styles.accountSectionIntro}>
          <h2>Profile</h2>
          <p>Your shared SanchezCloud profile as it appears in Scholight.</p>
        </div>
        <div className={styles.accountSectionBody}>
          {profile.error ? (
            <div className={styles.sectionError} role="alert">
              <span>Profile is unavailable.</span>
              <button type="button" onClick={() => void profile.refetch()}>
                Retry
              </button>
            </div>
          ) : (
            <div className={styles.profileSummary}>
              <SharedAvatar displayName={displayName} email={email} size="profile" />
              <div className={styles.profileIdentity}>
                <strong>{displayName}</strong>
                <span>{email || "—"}</span>
              </div>
              <p>Profile and password changes are managed in SanchezCloud Account.</p>
              <a className={styles.accountCenterLink} href={productConfig.accountCenter.url}>
                Manage SanchezCloud account
                <ExternalLinkIcon />
              </a>
            </div>
          )}
        </div>
      </section>
      <section className={styles.accountSection}>
        <div className={styles.accountSectionIntro}>
          <h2>Active sessions</h2>
          <p>Devices currently signed in to your Scholight account.</p>
          <PageRefreshButton
            label="active sessions"
            refreshing={sessions.isFetching}
            onRefresh={() => sessions.refetch()}
          />
        </div>
        <div className={styles.accountSectionBody}>
          {sessions.error && !sessions.data ? (
            <div className={styles.sectionError} role="alert">
              <span>
                {sessions.error instanceof ApiError
                  ? sessions.error.message
                  : "Sessions are temporarily unavailable."}
              </span>
              <button type="button" onClick={() => void sessions.refetch()}>
                Retry
              </button>
            </div>
          ) : sessions.isPending ? (
            <EditorialRowsSkeleton label="Loading active sessions" rows={2} />
          ) : orderedSessions.length === 0 ? (
            <div className={styles.sessionEmpty}>No active sessions were found.</div>
          ) : (
            <div className={styles.sessionList}>
              {orderedSessions.map((session, index) => (
                <m.div className={styles.sessionRow} key={session.id} {...ledgerRowMotion(index)}>
                  <div>
                    <strong>{parseUserAgent(session.user_agent ?? null)}</strong>
                    <span>{seenAt(session.last_seen_at, session.current, locale, messages)}</span>
                  </div>
                  {session.current ? (
                    <span className={styles.currentSession}>{messages.account.currentSession}</span>
                  ) : (
                    <button type="button" onClick={() => setPendingSession(session)}>
                      Revoke
                    </button>
                  )}
                </m.div>
              ))}
              {otherSessions.length > 0 && (
                <button
                  className={styles.revokeOthersButton}
                  type="button"
                  onClick={() => setRevokeOthersOpen(true)}
                >
                  Revoke other sessions
                </button>
              )}
            </div>
          )}
        </div>
      </section>
      <ConfirmDialog
        open={Boolean(pendingSession)}
        onOpenChange={(open) => {
          if (!open) {
            setPendingSession(null);
            revokeSession.reset();
          }
        }}
        title="Revoke this session?"
        description="This device will need to sign in again. Your current session will stay active."
        confirmLabel="Revoke"
        busyLabel="Revoking…"
        busy={revokeSession.isPending}
        error={
          revokeSession.error instanceof ApiError
            ? revokeSession.error.message
            : revokeSession.error
              ? "Unable to revoke this session."
              : undefined
        }
        onConfirm={() => pendingSession && revokeSession.mutate(pendingSession.id)}
      />
      <ConfirmDialog
        open={revokeOthersOpen}
        onOpenChange={(open) => {
          setRevokeOthersOpen(open);
          if (!open) revokeOthers.reset();
        }}
        title="Revoke other sessions?"
        description="Every other device will need to sign in again. This device will stay active."
        confirmLabel="Revoke all"
        busyLabel="Revoking…"
        busy={revokeOthers.isPending}
        error={
          revokeOthers.error instanceof ApiError
            ? revokeOthers.error.code === "session_context_unavailable"
              ? "Your current session cannot be identified. Sign in again and retry."
              : revokeOthers.error.message
            : revokeOthers.error
              ? "Unable to revoke other sessions."
              : undefined
        }
        onConfirm={() => revokeOthers.mutate()}
      />
    </main>
  );
}
