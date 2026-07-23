import * as Dialog from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { useEffect, useRef, useState } from "react";

import { ApiError } from "../../api/errors";
import type { AccessKey, CreatedAccessKey } from "../../api/types";
import { popoverMotion } from "../../app/motion";
import { EditorialSelect } from "../../components/EditorialSelect";
import { MotionDialogPortal } from "../../components/MotionDialog";
import { expiryFromPreset, type ExpiryPreset } from "../../lib/account";
import { styles } from "../../styles/classes";

const createExpiryOptions = [
  { value: "never", label: "Never" },
  { value: "30", label: "30 days from today" },
  { value: "90", label: "90 days from today" },
  { value: "365", label: "365 days from today" },
] as const;

const editExpiryOptions = [
  { value: "keep", label: "Keep current expiration" },
  ...createExpiryOptions,
] as const;

export function expiryValue(preset: ExpiryPreset): string | null | undefined {
  if (preset === "keep") return undefined;
  if (preset === "never") return null;
  return expiryFromPreset(preset);
}

export function AccessKeyFormDialog({
  open,
  mode,
  initialName = "",
  busy,
  error,
  onOpenChange,
  onSubmit,
}: {
  open: boolean;
  mode: "create" | "edit";
  initialName?: string;
  busy: boolean;
  error?: Error | null;
  onOpenChange: (open: boolean) => void;
  onSubmit: (name: string, expiry: ExpiryPreset) => void;
}) {
  const nameInput = useRef<HTMLInputElement>(null);
  const [name, setName] = useState(initialName);
  const [expiry, setExpiry] = useState<ExpiryPreset>(mode === "create" ? "never" : "keep");
  const normalized = name.trim();
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <MotionDialogPortal
        open={open}
        className={styles.formDialog}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          nameInput.current?.focus();
        }}
      >
        <p className={styles.dialogEyebrow}>
          {mode === "create" ? "NEW ACCESS KEY" : "ACCESS KEY"}
        </p>
        <Dialog.Title>{mode === "create" ? "Create a new key" : "Edit access key"}</Dialog.Title>
        <Dialog.Description>
          {mode === "create"
            ? "Use a clear name so you can identify where this key is used."
            : "Update the key name or choose a new expiration."}
        </Dialog.Description>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (normalized) onSubmit(normalized, expiry);
          }}
        >
          <label>
            Key name
            <input
              ref={nameInput}
              value={name}
              maxLength={64}
              onChange={(event) => setName(event.target.value)}
              placeholder="literature-review"
            />
          </label>
          <div className={styles.formSelectField}>
            <span>Expiration</span>
            <EditorialSelect
              label="Expiration"
              value={expiry}
              options={mode === "edit" ? editExpiryOptions : createExpiryOptions}
              onValueChange={setExpiry}
            />
          </div>
          {error && (
            <p className={styles.formMessageError} role="alert">
              {error instanceof ApiError ? error.message : "Unable to save this access key."}
            </p>
          )}
          <div className={styles.dialogActions}>
            <Dialog.Close className={styles.secondaryButton}>Cancel</Dialog.Close>
            <button className={styles.primaryButton} type="submit" disabled={busy || !normalized}>
              {busy ? "Saving…" : mode === "create" ? "Create key" : "Save changes"}
            </button>
          </div>
        </form>
      </MotionDialogPortal>
    </Dialog.Root>
  );
}

export function SecretDialog({ secret, onDone }: { secret: CreatedAccessKey; onDone: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const copyButton = useRef<HTMLButtonElement>(null);
  const resetTimer = useRef<number | undefined>(undefined);
  const [copyState, setCopyState] = useState<"idle" | "copying" | "copied" | "error">("idle");
  const message =
    copyState === "copied"
      ? "Access key copied."
      : copyState === "error"
        ? "Copy failed. The key is selected so you can copy it manually."
        : "";

  useEffect(() => () => window.clearTimeout(resetTimer.current), []);

  const copy = async () => {
    window.clearTimeout(resetTimer.current);
    setCopyState("copying");
    try {
      await navigator.clipboard.writeText(secret.key);
      setCopyState("copied");
      resetTimer.current = window.setTimeout(() => setCopyState("idle"), 1800);
    } catch {
      input.current?.focus();
      input.current?.select();
      setCopyState("error");
    }
  };
  return (
    <Dialog.Root open>
      <MotionDialogPortal
        open
        className={styles.secretDialog}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          copyButton.current?.focus();
        }}
        onEscapeKeyDown={(event) => event.preventDefault()}
        onPointerDownOutside={(event) => event.preventDefault()}
      >
        <p className={styles.dialogEyebrow}>ACCESS KEY CREATED</p>
        <Dialog.Title>Copy your key now</Dialog.Title>
        <Dialog.Description>
          For your security, this key will not be shown again.
        </Dialog.Description>
        <span className={styles.fieldCaption}>KEY NAME</span>
        <strong>{secret.name}</strong>
        <div className={styles.secretField}>
          <input
            ref={input}
            readOnly
            value={secret.key}
            onFocus={(event) => event.currentTarget.select()}
            aria-label="New access key"
          />
          <button
            ref={copyButton}
            className={styles.primaryButton}
            type="button"
            disabled={copyState === "copying"}
            aria-busy={copyState === "copying"}
            onClick={() => void copy()}
          >
            {copyState === "copying" ? "Copying…" : copyState === "copied" ? "Copied" : "Copy key"}
          </button>
        </div>
        <p className={styles.secretHint}>
          Store it in a password manager or server-side secret store.
        </p>
        {copyState === "error" && (
          <p className={styles.formMessageError} role="alert">
            {message}
          </p>
        )}
        <p className="sr-only" role="status" aria-live="polite">
          {copyState === "copied" ? message : ""}
        </p>
        <div className={styles.dialogActions}>
          <button className={styles.darkButton} type="button" onClick={onDone}>
            Done
          </button>
        </div>
      </MotionDialogPortal>
    </Dialog.Root>
  );
}

export function KeyActionsMenu({
  keyRecord,
  onEdit,
  onRevoke,
}: {
  keyRecord: AccessKey;
  onEdit: () => void;
  onRevoke: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <DropdownMenu.Root open={open} onOpenChange={setOpen}>
      <DropdownMenu.Trigger
        className={styles.rowMenuTrigger}
        aria-label={`Actions for ${keyRecord.name}`}
      >
        •••
      </DropdownMenu.Trigger>
      <AnimatePresence>
        {open && (
          <DropdownMenu.Portal forceMount>
            <DropdownMenu.Content asChild forceMount align="end">
              <m.div className={styles.rowMenu} {...popoverMotion}>
                <DropdownMenu.Item className={styles.rowMenuItem} onSelect={onEdit}>
                  Edit
                </DropdownMenu.Item>
                <DropdownMenu.Item className={styles.rowMenuDanger} onSelect={onRevoke}>
                  Revoke
                </DropdownMenu.Item>
              </m.div>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        )}
      </AnimatePresence>
    </DropdownMenu.Root>
  );
}
