import * as Dialog from "@radix-ui/react-dialog";
import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";

import type { SurveyQuota } from "../../api/types";
import { routes, withQuery } from "../../app/routes";
import { MotionDialogPortal } from "../../components/MotionDialog";
import { styles } from "../../styles/classes";

function SurveyDialog({
  open,
  eyebrow,
  title,
  description,
  primaryLabel,
  secondaryLabel = "Cancel",
  danger = false,
  busy = false,
  error,
  children,
  onOpenChange,
  onPrimary,
}: {
  open: boolean;
  eyebrow: string;
  title: string;
  description: string;
  primaryLabel: string;
  secondaryLabel?: string;
  danger?: boolean;
  busy?: boolean;
  error?: string;
  children?: ReactNode;
  onOpenChange: (open: boolean) => void;
  onPrimary: () => void;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <MotionDialogPortal open={open} className={styles.dialogContent}>
        <p className={styles.dialogEyebrow}>{eyebrow}</p>
        <Dialog.Title>{title}</Dialog.Title>
        <Dialog.Description>{description}</Dialog.Description>
        {children}
        {error && <p className={styles.dialogError}>{error}</p>}
        <div className={styles.dialogActions}>
          <Dialog.Close className={styles.secondaryButton}>{secondaryLabel}</Dialog.Close>
          <button
            className={danger ? styles.dangerButton : styles.primaryButton}
            type="button"
            disabled={busy}
            onClick={onPrimary}
          >
            {busy ? "Working…" : primaryLabel}
          </button>
        </div>
      </MotionDialogPortal>
    </Dialog.Root>
  );
}

export function SurveySignInDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const navigate = useNavigate();
  return (
    <SurveyDialog
      open={open}
      eyebrow="SCHOLIGHT SURVEY"
      title="Sign in to start a survey"
      description="Survey drafts, progress, and completed reports are saved to your Scholight account."
      primaryLabel="Sign in"
      onOpenChange={onOpenChange}
      onPrimary={() => navigate(withQuery(routes.login.path, { returnTo: routes.survey.path }))}
    />
  );
}

export function SurveyLimitDialog({
  open,
  quota,
  onOpenChange,
  onReview,
}: {
  open: boolean;
  quota?: SurveyQuota;
  onOpenChange: (open: boolean) => void;
  onReview: () => void;
}) {
  const detail = quota
    ? `${quota.succeeded + quota.reserved} of ${quota.daily_limit} survey slots are currently in use today.`
    : "Today’s survey allowance is currently full.";
  return (
    <SurveyDialog
      open={open}
      eyebrow="SURVEY LIMIT"
      title="Start another survey later"
      description={`${detail} Review your active surveys or return after the daily allowance resets.`}
      primaryLabel="Review active"
      secondaryLabel="Close"
      onOpenChange={onOpenChange}
      onPrimary={onReview}
    />
  );
}

export function SurveyCancelDialog({
  open,
  busy,
  error,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  busy: boolean;
  error?: string;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  return (
    <SurveyDialog
      open={open}
      eyebrow="CANCEL SURVEY"
      title="Cancel this survey?"
      description="The current draft or research run will stop and the survey will leave your active list. This action cannot be undone."
      primaryLabel="Cancel survey"
      secondaryLabel="Keep survey"
      danger
      busy={busy}
      error={error}
      onOpenChange={onOpenChange}
      onPrimary={onConfirm}
    />
  );
}

export function SurveyStartDialog({
  open,
  busy,
  error,
  notifyOnCompletion,
  onNotifyChange,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  busy: boolean;
  error?: string;
  notifyOnCompletion: boolean;
  onNotifyChange: (notify: boolean) => void;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  return (
    <SurveyDialog
      open={open}
      eyebrow="START SURVEY"
      title="Start this research survey?"
      description="The current research brief will be used to start the survey. It cannot be edited after research begins."
      primaryLabel="Approve & start"
      secondaryLabel="Go back"
      busy={busy}
      error={error}
      onOpenChange={onOpenChange}
      onPrimary={onConfirm}
    >
      <label className={styles.surveyNotificationChoice}>
        <input
          type="checkbox"
          aria-labelledby="survey-notification-label"
          aria-describedby="survey-notification-description"
          checked={notifyOnCompletion}
          disabled={busy}
          onChange={(event) => onNotifyChange(event.target.checked)}
        />
        <span>
          <strong id="survey-notification-label">Email me when this survey finishes</strong>
          <small id="survey-notification-description">
            Send one email to my Scholight account when the report is ready or if the survey fails.
          </small>
        </span>
      </label>
    </SurveyDialog>
  );
}
