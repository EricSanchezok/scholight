import * as Dialog from "@radix-ui/react-dialog";
import { useState } from "react";

import { ApiError } from "../../api/errors";
import { MotionDialogPortal } from "../../components/MotionDialog";
import { styles } from "../../styles/classes";

export function DeleteAccountDialog({
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
