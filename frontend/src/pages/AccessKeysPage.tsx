import * as Dialog from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { useRef, useState } from "react";

import { accessKeyApi } from "../api/domain";
import { ApiError } from "../api/errors";
import type { AccessKey, CreatedAccessKey } from "../api/types";
import { popoverMotion } from "../app/motion";
import { queryKeys } from "../app/queryKeys";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EditorialRowsSkeleton } from "../components/EditorialSkeleton";
import { EditorialSelect } from "../components/EditorialSelect";
import { MotionDialogPortal } from "../components/MotionDialog";
import { accessKeyStatus, expiryFromPreset, type ExpiryPreset } from "../lib/account";
import styles from "../styles/app.module.css";

function date(value: string): string {
  return new Intl.DateTimeFormat("en", { day: "2-digit", month: "short", year: "numeric" }).format(
    new Date(value),
  );
}

function lastUsed(value: string | null): string {
  if (!value) return "Never";
  const parsed = new Date(value);
  const today = new Date();
  if (parsed.toDateString() === today.toDateString())
    return `Today, ${new Intl.DateTimeFormat("en", { hour: "2-digit", minute: "2-digit" }).format(parsed)}`;
  return new Intl.DateTimeFormat("en", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function expiryValue(preset: ExpiryPreset): string | null | undefined {
  if (preset === "keep") return undefined;
  if (preset === "never") return null;
  return expiryFromPreset(preset);
}

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

function AccessKeyFormDialog({
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

function SecretDialog({ secret, onDone }: { secret: CreatedAccessKey; onDone: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const copyButton = useRef<HTMLButtonElement>(null);
  const [message, setMessage] = useState("");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(secret.key);
      setMessage("Access key copied.");
    } catch {
      input.current?.focus();
      input.current?.select();
      setMessage("Copy failed. The key is selected so you can copy it manually.");
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
            onClick={() => void copy()}
          >
            Copy key
          </button>
        </div>
        <p className={styles.secretHint}>
          Store it in a password manager or server-side secret store.
        </p>
        <p className="sr-only" aria-live="polite">
          {message}
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

function KeyActionsMenu({
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

export function AccessKeysPage() {
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [editKey, setEditKey] = useState<AccessKey | null>(null);
  const [revokeKey, setRevokeKey] = useState<AccessKey | null>(null);
  const [secret, setSecret] = useState<CreatedAccessKey | null>(null);
  const keys = useQuery({
    queryKey: queryKeys.accessKeys,
    queryFn: accessKeyApi.list,
    retry: false,
  });
  const visible = (keys.data ?? []).filter((key) => accessKeyStatus(key) !== "revoked");
  const activeCount = visible.filter((key) => accessKeyStatus(key) === "active").length;
  const refresh = () => queryClient.invalidateQueries({ queryKey: queryKeys.accessKeys });
  const create = useMutation({
    mutationFn: ({ name, expiry }: { name: string; expiry: ExpiryPreset }) =>
      accessKeyApi.create({ name, scopes: ["search"], expires_at: expiryValue(expiry) ?? null }),
    onSuccess: async (data) => {
      setCreateOpen(false);
      setSecret(data);
      await refresh();
    },
  });
  const update = useMutation({
    mutationFn: ({ key, name, expiry }: { key: AccessKey; name: string; expiry: ExpiryPreset }) => {
      const expiresAt = expiryValue(expiry);
      return accessKeyApi.update(key.id, {
        name,
        ...(expiresAt !== undefined ? { expires_at: expiresAt } : {}),
      });
    },
    onSuccess: async () => {
      setEditKey(null);
      await refresh();
    },
  });
  const revoke = useMutation({
    mutationFn: (key: AccessKey) => accessKeyApi.revoke(key.id),
    onSuccess: async () => {
      setRevokeKey(null);
      await refresh();
    },
  });

  return (
    <main className={styles.ledgerPage}>
      <header className={styles.ledgerHeading}>
        <h1>Access keys</h1>
        <p>Create keys for tools and agents that search Scholight on your behalf.</p>
      </header>
      <section className={styles.keyLedger} aria-labelledby="access-key-count">
        <div className={styles.keyLedgerHeading}>
          <h2 id="access-key-count">
            {activeCount} active {activeCount === 1 ? "key" : "keys"}
          </h2>
          <button
            className={styles.primaryButton}
            type="button"
            disabled={activeCount >= 10}
            onClick={() => setCreateOpen(true)}
          >
            Create new key
          </button>
        </div>
        {keys.error && !keys.data ? (
          <div className={styles.sectionError} role="alert">
            <span>
              {keys.error instanceof ApiError
                ? keys.error.message
                : "Access keys are temporarily unavailable."}
            </span>
            <button type="button" onClick={() => void keys.refetch()}>
              Retry
            </button>
          </div>
        ) : keys.isPending ? (
          <EditorialRowsSkeleton label="Loading access keys" rows={3} />
        ) : visible.length === 0 ? (
          <div className={styles.ledgerEmpty}>
            <h3>No access keys</h3>
            <p>Create a key when a trusted tool or agent needs to search Scholight.</p>
          </div>
        ) : (
          <div className={styles.keyTableWrap}>
            <table className={styles.keyTable}>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Key</th>
                  <th>Created</th>
                  <th>Last used</th>
                  <th>Status</th>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((key, index) => {
                  const status = accessKeyStatus(key);
                  return (
                    <m.tr
                      key={key.id}
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ duration: 0.16, delay: Math.min(index * 0.02, 0.16) }}
                    >
                      <td>{key.name}</td>
                      <td>
                        <code>sk_live_••••••••{key.last4}</code>
                      </td>
                      <td>{date(key.created_at)}</td>
                      <td>{lastUsed(key.last_used_at)}</td>
                      <td>
                        <span
                          className={status === "expired" ? styles.keyExpired : styles.keyActive}
                        >
                          {status === "expired" ? "Expired" : "Active"}
                        </span>
                        {key.expires_at && status === "active" && (
                          <small>Expires {date(key.expires_at)}</small>
                        )}
                      </td>
                      <td>
                        <KeyActionsMenu
                          keyRecord={key}
                          onEdit={() => setEditKey(key)}
                          onRevoke={() => setRevokeKey(key)}
                        />
                      </td>
                    </m.tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {activeCount >= 10 && (
          <p className={styles.formMessageError}>You have reached the limit of 10 active keys.</p>
        )}
      </section>
      <p className={styles.securityNote}>
        Access keys are shown only once. Store them securely and never include them in browser code.
      </p>
      {createOpen && (
        <AccessKeyFormDialog
          open
          mode="create"
          busy={create.isPending}
          error={create.error}
          onOpenChange={(open) => {
            setCreateOpen(open);
            if (!open) create.reset();
          }}
          onSubmit={(name, expiry) => create.mutate({ name, expiry })}
        />
      )}
      {editKey && (
        <AccessKeyFormDialog
          key={editKey.id}
          open
          mode="edit"
          initialName={editKey.name}
          busy={update.isPending}
          error={update.error}
          onOpenChange={(open) => {
            if (!open) {
              setEditKey(null);
              update.reset();
            }
          }}
          onSubmit={(name, expiry) => update.mutate({ key: editKey, name, expiry })}
        />
      )}
      {secret && (
        <SecretDialog
          secret={secret}
          onDone={() => {
            setSecret(null);
            create.reset();
          }}
        />
      )}
      <ConfirmDialog
        open={Boolean(revokeKey)}
        onOpenChange={(open) => {
          if (!open) {
            setRevokeKey(null);
            revoke.reset();
          }
        }}
        title="Revoke this access key?"
        description={`The ${revokeKey?.name ?? "selected"} key will stop working immediately. This action cannot be undone.`}
        confirmLabel="Revoke"
        busyLabel="Revoking…"
        busy={revoke.isPending}
        error={
          revoke.error instanceof ApiError
            ? revoke.error.message
            : revoke.error
              ? "Unable to revoke this access key."
              : undefined
        }
        onConfirm={() => revokeKey && revoke.mutate(revokeKey)}
      />
    </main>
  );
}
