import * as Dialog from "@radix-ui/react-dialog";

import { styles } from "../styles/classes";
import { MotionDialogPortal } from "./MotionDialog";

interface Props {
  open: boolean;
  title: string;
  description: string;
  busy?: boolean;
  error?: string;
  confirmLabel?: string;
  busyLabel?: string;
  tone?: "danger" | "primary";
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  busy,
  error,
  confirmLabel = "Delete",
  busyLabel = "Deleting…",
  tone = "danger",
  onOpenChange,
  onConfirm,
}: Props) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <MotionDialogPortal open={open} className={styles.dialogContent}>
        <div className={styles.dialogRule} />
        <Dialog.Title>{title}</Dialog.Title>
        <Dialog.Description>{description}</Dialog.Description>
        {error && (
          <p className={styles.dialogError} role="alert">
            {error}
          </p>
        )}
        <div className={styles.dialogActions}>
          <Dialog.Close className={styles.secondaryButton}>Cancel</Dialog.Close>
          <button
            className={tone === "primary" ? styles.primaryButton : styles.dangerButton}
            type="button"
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? busyLabel : confirmLabel}
          </button>
        </div>
      </MotionDialogPortal>
    </Dialog.Root>
  );
}
