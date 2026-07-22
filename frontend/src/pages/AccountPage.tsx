import * as Dialog from "@radix-ui/react-dialog";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as m from "motion/react-m";
import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { useNavigate } from "react-router-dom";
import { z } from "zod";

import { accountApi, authApi } from "../api/domain";
import { ApiError } from "../api/errors";
import type { Session } from "../api/types";
import { queryKeys } from "../app/queryKeys";
import { routes, withQuery } from "../app/routes";
import { ledgerRowMotion } from "../app/motion";
import { useAuth } from "../auth/context";
import { clearSession } from "../auth/session";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EditorialRowsSkeleton } from "../components/EditorialSkeleton";
import { MotionDialogPortal } from "../components/MotionDialog";
import { formatFullDateTime } from "../i18n/format";
import { useI18n, type AppLocale } from "../i18n/I18nProvider";
import type { Messages } from "../i18n/en";
import { parseUserAgent, sortSessions } from "../lib/account";
import styles from "../styles/app.module.css";

const passwordSchema = z
  .object({
    currentPassword: z.string().min(1, "Enter your current password."),
    newPassword: z.string().min(12, "Use at least 12 characters."),
    confirmPassword: z.string(),
  })
  .refine((value) => value.newPassword === value.confirmPassword, {
    path: ["confirmPassword"],
    message: "Passwords do not match.",
  });

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

function DeleteAccountDialog({
  open,
  busy,
  error,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  busy: boolean;
  error?: Error | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: (password: string, confirmation: string) => void;
}) {
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <MotionDialogPortal open={open} className={styles.formDialog}>
        <p className={styles.dialogDangerEyebrow}>PERMANENT ACTION</p>
        <Dialog.Title>Delete your account?</Dialog.Title>
        <Dialog.Description>
          This permanently removes your search history, access keys, usage records, and account
          access.
        </Dialog.Description>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onConfirm(password, confirmation);
          }}
        >
          <label>
            Current password
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          <label>
            Type DELETE to confirm
            <input
              value={confirmation}
              autoComplete="off"
              onChange={(event) => setConfirmation(event.target.value)}
            />
          </label>
          {error && (
            <p className={styles.formMessageError} role="alert">
              {error instanceof ApiError ? error.message : "Unable to delete your account."}
            </p>
          )}
          <div className={styles.dialogActions}>
            <Dialog.Close className={styles.secondaryButton}>Cancel</Dialog.Close>
            <button
              className={styles.dangerButton}
              disabled={busy || !password || confirmation !== "DELETE"}
            >
              {busy ? "Deleting…" : "Delete account"}
            </button>
          </div>
        </form>
      </MotionDialogPortal>
    </Dialog.Root>
  );
}

export function AccountPage() {
  const { locale, messages } = useI18n();
  const { user, refreshProfile } = useAuth();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [passwordMessage, setPasswordMessage] = useState("");
  const [pendingSession, setPendingSession] = useState<Session | null>(null);
  const [revokeOthersOpen, setRevokeOthersOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
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
  useEffect(() => setDisplayName(profile.data?.display_name ?? ""), [profile.data?.display_name]);
  const updateProfile = useMutation({
    mutationFn: () => accountApi.updateProfile(displayName.trim() || null),
    onSuccess: async (data) => {
      queryClient.setQueryData(queryKeys.profile, data);
      await refreshProfile();
    },
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
  const deleteAccount = useMutation({
    mutationFn: ({ password, confirmation }: { password: string; confirmation: string }) =>
      accountApi.deleteAccount({ password, confirmation }),
    onSuccess: () => {
      clearSession();
      queryClient.clear();
      navigate(routes.home.path, { replace: true });
    },
  });
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isSubmitting },
  } = useForm<z.infer<typeof passwordSchema>>({ resolver: zodResolver(passwordSchema) });
  const changePassword = handleSubmit(async ({ currentPassword, newPassword }) => {
    try {
      setPasswordMessage("");
      await authApi.changePassword(currentPassword, newPassword);
      clearSession();
      queryClient.clear();
      reset();
      navigate(withQuery(routes.login.path, { password: "changed" }), { replace: true });
    } catch (error) {
      setPasswordMessage(
        error instanceof ApiError ? error.message : "Unable to change your password.",
      );
    }
  });
  const orderedSessions = sortSessions(sessions.data ?? []);
  const otherSessions = orderedSessions.filter((session) => !session.current);

  return (
    <main className={styles.accountPage}>
      <header className={styles.accountIntro}>
        <p>ACCOUNT</p>
        <h1>Account settings</h1>
        <span>Manage your profile, security, and active sessions.</span>
      </header>
      <section className={styles.accountSection}>
        <div className={styles.accountSectionIntro}>
          <h2>Profile</h2>
          <p>How your name appears across Scholight.</p>
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
            <form
              className={styles.accountForm}
              onSubmit={(event) => {
                event.preventDefault();
                updateProfile.mutate();
              }}
            >
              <label>
                Display name
                <input
                  value={displayName}
                  onChange={(event) => setDisplayName(event.target.value)}
                  maxLength={120}
                />
              </label>
              <div>
                <span className={styles.fieldCaption}>EMAIL</span>
                <p>{profile.data?.email ?? "—"}</p>
              </div>
              {updateProfile.error && (
                <p className={styles.formMessageError} role="alert">
                  Could not update your profile.
                </p>
              )}
              <button className={styles.primaryButton} disabled={updateProfile.isPending}>
                {updateProfile.isPending ? "Saving…" : "Save changes"}
              </button>
            </form>
          )}
        </div>
      </section>
      <section className={styles.accountSection}>
        <div className={styles.accountSectionIntro}>
          <h2>Password</h2>
          <p>Use at least 12 characters. Updating your password signs you out everywhere.</p>
        </div>
        <div className={styles.accountSectionBody}>
          <form className={styles.passwordForm} onSubmit={changePassword}>
            <label className={styles.fullField}>
              <span className="sr-only">Current password</span>
              <input
                type="password"
                placeholder="Current password"
                autoComplete="current-password"
                {...register("currentPassword")}
              />
              {errors.currentPassword && <small>{errors.currentPassword.message}</small>}
            </label>
            <label>
              <span className="sr-only">New password</span>
              <input
                type="password"
                placeholder="New password"
                autoComplete="new-password"
                {...register("newPassword")}
              />
              {errors.newPassword && <small>{errors.newPassword.message}</small>}
            </label>
            <label>
              <span className="sr-only">Confirm new password</span>
              <input
                type="password"
                placeholder="Confirm new password"
                autoComplete="new-password"
                {...register("confirmPassword")}
              />
              {errors.confirmPassword && <small>{errors.confirmPassword.message}</small>}
            </label>
            <p className={styles.passwordHint}>
              A strong password is long, unique, and not reused elsewhere.
            </p>
            {passwordMessage && (
              <p className={styles.formMessageError} role="alert">
                {passwordMessage}
              </p>
            )}
            <button className={styles.darkButton} disabled={isSubmitting}>
              {isSubmitting ? "Changing…" : "Change password"}
            </button>
          </form>
        </div>
      </section>
      <section className={styles.accountSection}>
        <div className={styles.accountSectionIntro}>
          <h2>Active sessions</h2>
          <p>Devices currently signed in to your Scholight account.</p>
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
                    <strong>{parseUserAgent(session.user_agent)}</strong>
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
      <section className={`${styles.accountSection} ${styles.dangerSection}`}>
        <div className={styles.accountSectionIntro}>
          <h2>Delete account</h2>
          <p>Permanently remove your account, search history, access keys, and usage records.</p>
        </div>
        <div className={styles.accountSectionBody}>
          <button
            className={styles.deleteAccountButton}
            type="button"
            onClick={() => setDeleteOpen(true)}
          >
            Delete account
          </button>
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
      <DeleteAccountDialog
        open={deleteOpen}
        busy={deleteAccount.isPending}
        error={deleteAccount.error}
        onOpenChange={(open) => {
          setDeleteOpen(open);
          if (!open) deleteAccount.reset();
        }}
        onConfirm={(password, confirmation) => deleteAccount.mutate({ password, confirmation })}
      />
    </main>
  );
}
