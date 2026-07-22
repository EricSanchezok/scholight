import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { useNavigate } from "react-router-dom";
import { z } from "zod";

import { accountApi, authApi } from "../api/domain";
import { ApiError } from "../api/errors";
import { queryKeys } from "../app/queryKeys";
import { useAuth } from "../auth/AuthProvider";
import { clearSession } from "../auth/session";
import { formatDate } from "../lib/format";
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

export function AccountPage() {
  const { user, refreshProfile } = useAuth();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [passwordMessage, setPasswordMessage] = useState("");
  const profile = useQuery({
    queryKey: queryKeys.profile,
    queryFn: accountApi.profile,
    initialData: user ?? undefined,
    retry: false,
  });
  const quotas = useQuery({ queryKey: queryKeys.quotas, queryFn: accountApi.quotas, retry: false });
  const updateProfile = useMutation({
    mutationFn: () => accountApi.updateProfile(displayName.trim() || null),
    onSuccess: async (data) => {
      queryClient.setQueryData(queryKeys.profile, data);
      await refreshProfile();
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
      navigate("/login?password=changed", { replace: true });
    } catch (error) {
      setPasswordMessage(
        error instanceof ApiError ? error.message : "Unable to change your password.",
      );
    }
  });
  const visibleQuotas =
    quotas.data?.filter(
      (quota) => quota.operation === "search_level1" || quota.operation === "search_level2",
    ) ?? [];

  return (
    <main className={styles.accountPage}>
      <header className={styles.pageHeading}>
        <div className={styles.accentLine} />
        <p className={styles.eyebrow}>Account</p>
        <h1>Account settings</h1>
        <p>Manage your identity, daily search usage, and password.</p>
      </header>
      <div className={styles.accountSections}>
        <section>
          <div className={styles.sectionLabel}>
            <span>01</span>
            <h2>Profile</h2>
          </div>
          {profile.error ? (
            <div className={styles.inlineError}>
              Profile is unavailable.{" "}
              <button type="button" onClick={() => void profile.refetch()}>
                Retry
              </button>
            </div>
          ) : (
            <form
              className={styles.settingsForm}
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
              <dl className={styles.readonlyDetails}>
                <div>
                  <dt>Email</dt>
                  <dd>{profile.data?.email}</dd>
                </div>
                <div>
                  <dt>Status</dt>
                  <dd>{profile.data?.status.replaceAll("_", " ")}</dd>
                </div>
                <div>
                  <dt>Member since</dt>
                  <dd>{profile.data?.created_at ? formatDate(profile.data.created_at) : "—"}</dd>
                </div>
              </dl>
              {updateProfile.error && (
                <p className={styles.formMessageError} role="alert">
                  Could not update your profile.
                </p>
              )}
              <button className={styles.primaryButton} disabled={updateProfile.isPending}>
                Save profile
              </button>
            </form>
          )}
        </section>
        <section>
          <div className={styles.sectionLabel}>
            <span>02</span>
            <h2>Daily usage</h2>
          </div>
          {quotas.error ? (
            <div className={styles.inlineError}>
              Usage is unavailable.{" "}
              <button type="button" onClick={() => void quotas.refetch()}>
                Retry
              </button>
            </div>
          ) : (
            <div className={styles.quotaList}>
              {visibleQuotas.map((quota) => (
                <div key={quota.operation} className={styles.quotaRow}>
                  <div>
                    <strong>{quota.operation === "search_level1" ? "Standard" : "Thorough"}</strong>
                    <span>{quota.remaining} remaining today</span>
                  </div>
                  <div className={styles.quotaTrack}>
                    <span
                      style={{
                        width: `${quota.daily_limit ? Math.min(100, (quota.used / quota.daily_limit) * 100) : 0}%`,
                      }}
                    />
                  </div>
                  <p>
                    {quota.used} used / {quota.daily_limit} daily limit
                  </p>
                </div>
              ))}
            </div>
          )}
        </section>
        <section>
          <div className={styles.sectionLabel}>
            <span>03</span>
            <h2>Security</h2>
          </div>
          <form className={styles.settingsForm} onSubmit={changePassword}>
            <label>
              Current password
              <input
                type="password"
                autoComplete="current-password"
                {...register("currentPassword")}
              />
              {errors.currentPassword && <span>{errors.currentPassword.message}</span>}
            </label>
            <label>
              New password
              <input type="password" autoComplete="new-password" {...register("newPassword")} />
              {errors.newPassword && <span>{errors.newPassword.message}</span>}
            </label>
            <label>
              Confirm new password
              <input type="password" autoComplete="new-password" {...register("confirmPassword")} />
              {errors.confirmPassword && <span>{errors.confirmPassword.message}</span>}
            </label>
            {passwordMessage && (
              <p className={styles.formMessageError} role="alert">
                {passwordMessage}
              </p>
            )}
            <button className={styles.primaryButton} disabled={isSubmitting}>
              Change password
            </button>
            <p className={styles.settingsHint}>
              You’ll be signed out on every device after your password changes.
            </p>
          </form>
        </section>
      </div>
    </main>
  );
}
