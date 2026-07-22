import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as m from "motion/react-m";
import { useState } from "react";

import { accessKeyApi } from "../api/domain";
import { ApiError } from "../api/errors";
import type { AccessKey, CreatedAccessKey } from "../api/types";
import { productConfig } from "../config/product";
import { ledgerRowMotion } from "../app/motion";
import { queryKeys } from "../app/queryKeys";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EditorialRowsSkeleton } from "../components/EditorialSkeleton";
import {
  AccessKeyFormDialog,
  expiryValue,
  KeyActionsMenu,
  SecretDialog,
} from "../features/access-keys/AccessKeyOverlays";
import { formatCalendarDate, formatCompactDateTime, formatTime } from "../i18n/format";
import { useI18n, type AppLocale } from "../i18n/I18nProvider";
import { accessKeyStatus, type ExpiryPreset } from "../lib/account";
import { styles } from "../styles/classes";

function date(value: string, locale: AppLocale): string {
  return formatCalendarDate(value, locale);
}

function lastUsed(
  value: string | null,
  locale: AppLocale,
  never: string,
  todayAt: (value: string) => string,
): string {
  if (!value) return never;
  const parsed = new Date(value);
  const today = new Date();
  if (parsed.toDateString() === today.toDateString()) return todayAt(formatTime(parsed, locale));
  return formatCompactDateTime(parsed, locale);
}

export function AccessKeysPage() {
  const { locale, messages } = useI18n();
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [editKey, setEditKey] = useState<AccessKey | null>(null);
  const [revokeKey, setRevokeKey] = useState<AccessKey | null>(null);
  const [secret, setSecret] = useState<CreatedAccessKey | null>(null);
  const keys = useQuery({
    queryKey: queryKeys.accessKeys,
    queryFn: accessKeyApi.list,
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
            disabled={activeCount >= productConfig.accessKeys.maxActive}
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
                  <th>{messages.accessKeys.created}</th>
                  <th>{messages.accessKeys.lastUsed}</th>
                  <th>{messages.accessKeys.status}</th>
                  <th>
                    <span className="sr-only">{messages.common.actions}</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((key, index) => {
                  const status = accessKeyStatus(key);
                  return (
                    <m.tr key={key.id} {...ledgerRowMotion(index)}>
                      <td>{key.name}</td>
                      <td>
                        <code>sk_live_••••••••{key.last4}</code>
                      </td>
                      <td>{date(key.created_at, locale)}</td>
                      <td>
                        {lastUsed(
                          key.last_used_at,
                          locale,
                          messages.common.never,
                          messages.accessKeys.todayAt,
                        )}
                      </td>
                      <td>
                        <span
                          className={status === "expired" ? styles.keyExpired : styles.keyActive}
                        >
                          {status === "expired" ? messages.common.expired : messages.common.active}
                        </span>
                        {key.expires_at && status === "active" && (
                          <small>{messages.accessKeys.expires(date(key.expires_at, locale))}</small>
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
        {activeCount >= productConfig.accessKeys.maxActive && (
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
