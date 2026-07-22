import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as m from "motion/react-m";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { historyApi } from "../api/domain";
import { ApiError } from "../api/errors";
import { queryKeys } from "../app/queryKeys";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EditorialRowsSkeleton } from "../components/EditorialSkeleton";
import { DeleteSearchIcon, SearchIcon, TrashIcon } from "../components/icons";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { buildSearchUrl } from "../lib/format";
import styles from "../styles/app.module.css";

const PAGE_SIZE = 10;

function formatHistoryDate(value: string) {
  return new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

export function HistoryPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const page = Math.max(1, Number(params.get("page")) || 1);
  const urlFilter = params.get("q") ?? "";
  const [filter, setFilter] = useState(urlFilter);
  const debouncedFilter = useDebouncedValue(filter, 300);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [pendingDelete, setPendingDelete] = useState<number[]>([]);
  const checkbox = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (debouncedFilter === urlFilter) return;
    const next = new URLSearchParams(params);
    if (debouncedFilter) next.set("q", debouncedFilter);
    else next.delete("q");
    next.delete("page");
    setParams(next, { replace: true });
  }, [debouncedFilter, params, setParams, urlFilter]);
  useEffect(() => setSelected(new Set()), [page, urlFilter]);

  const history = useQuery({
    queryKey: queryKeys.history(urlFilter, page),
    queryFn: () => historyApi.list(PAGE_SIZE, (page - 1) * PAGE_SIZE, urlFilter || undefined),
    placeholderData: (previous) => previous,
    retry: false,
  });
  const items = history.data?.items ?? [];
  const allSelected = items.length > 0 && items.every((item) => selected.has(item.id));
  const someSelected = items.some((item) => selected.has(item.id)) && !allSelected;
  useEffect(() => {
    if (checkbox.current) checkbox.current.indeterminate = someSelected;
  }, [someSelected]);

  const remove = useMutation({
    mutationFn: async (ids: number[]) =>
      ids.length === 1 && ids[0] !== undefined
        ? historyApi.remove(ids[0])
        : historyApi.removeMany(ids),
    onSuccess: async () => {
      setSelected(new Set());
      setPendingDelete([]);
      await queryClient.invalidateQueries({ queryKey: ["private", "history"] });
      const remainingOnPage = items.length - pendingDelete.length;
      if (remainingOnPage <= 0 && page > 1) {
        const next = new URLSearchParams(params);
        next.set("page", String(page - 1));
        setParams(next, { replace: true });
      }
    },
  });
  const pageCount = Math.max(1, Math.ceil((history.data?.total ?? 0) / PAGE_SIZE));
  const goToPage = (nextPage: number) => {
    const next = new URLSearchParams(params);
    if (nextPage <= 1) next.delete("page");
    else next.set("page", String(nextPage));
    setParams(next);
  };

  return (
    <main className={styles.historyPage}>
      <header className={styles.historyHeading}>
        <h1>Search history</h1>
        <p>Revisit your previous research questions or remove the searches you no longer need.</p>
      </header>
      <section aria-label="Search history">
        <div className={styles.historyToolbar}>
          <label className={styles.selectionSummary}>
            <input
              ref={checkbox}
              type="checkbox"
              checked={allSelected}
              onChange={() =>
                setSelected(allSelected ? new Set() : new Set(items.map((item) => item.id)))
              }
            />
            <span>
              <strong>
                {selected.size > 0 ? `${selected.size} selected` : "Select this page"}
              </strong>
              {` of ${history.data?.total ?? 0} searches`}
            </span>
          </label>
          <div className={styles.historyToolbarActions}>
            <label className={styles.filterInput}>
              <SearchIcon />
              <span className="sr-only">Filter search history</span>
              <input
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                placeholder="Filter searches"
              />
            </label>
            {selected.size > 0 && (
              <button
                className={styles.deleteSelection}
                type="button"
                onClick={() => setPendingDelete([...selected])}
              >
                <TrashIcon /> Delete selected
              </button>
            )}
          </div>
        </div>
        {history.error && (
          <div className={styles.noticeError} role="alert">
            <span>
              {history.error instanceof ApiError
                ? history.error.message
                : "History is temporarily unavailable."}
            </span>
            <button type="button" onClick={() => void history.refetch()}>
              Retry
            </button>
          </div>
        )}
        {history.isPending ? (
          <EditorialRowsSkeleton label="Loading search history" rows={4} />
        ) : !history.error && items.length === 0 ? (
          <div className={styles.emptyHistory}>
            <div className={styles.emptyMark} aria-hidden="true">
              <SearchIcon />
            </div>
            <h2>{urlFilter ? "No matching searches" : "No searches yet"}</h2>
            <p>
              {urlFilter
                ? "No saved query matches this filter."
                : "Searches you make while signed in will appear here."}
            </p>
            {urlFilter ? (
              <button
                className={styles.secondaryButton}
                type="button"
                onClick={() => setFilter("")}
              >
                Clear filter
              </button>
            ) : (
              <button className={styles.primaryButton} type="button" onClick={() => navigate("/")}>
                Start searching
              </button>
            )}
          </div>
        ) : (
          <>
            <div className={styles.historyList} aria-busy={history.isFetching}>
              {items.map((item, index) => (
                <m.article
                  key={item.id}
                  className={`${styles.historyRow} ${selected.has(item.id) ? styles.historyRowSelected : ""}`}
                  initial={{ opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.16, delay: Math.min(index * 0.02, 0.16) }}
                >
                  <label className={styles.rowCheck}>
                    <input
                      type="checkbox"
                      checked={selected.has(item.id)}
                      onChange={() =>
                        setSelected((current) => {
                          const next = new Set(current);
                          if (next.has(item.id)) next.delete(item.id);
                          else next.add(item.id);
                          return next;
                        })
                      }
                    />
                    <span className="sr-only">Select {item.query}</span>
                  </label>
                  <div className={styles.historyDetails}>
                    <h2>{item.query}</h2>
                    <p>
                      <time dateTime={item.created_at}>{formatHistoryDate(item.created_at)}</time>
                      {` · ${item.result_count} results · ${(item.elapsed_ms / 1000).toFixed(2)}s`}
                    </p>
                    {(item.filters.categories?.length ||
                      item.filters.authors?.length ||
                      item.filters.date_from ||
                      item.filters.date_to) && (
                      <p className={styles.historyFilters}>Filtered search</p>
                    )}
                  </div>
                  <div className={styles.historyActions}>
                    <span>{item.strength === "thorough" ? "Thorough" : "Standard"}</span>
                    <button
                      type="button"
                      onClick={() =>
                        navigate(
                          buildSearchUrl({
                            query: item.query,
                            strength: item.strength,
                            filters: item.filters,
                          }),
                        )
                      }
                    >
                      Search again
                    </button>
                    <button
                      type="button"
                      aria-label={`Delete ${item.query}`}
                      onClick={() => setPendingDelete([item.id])}
                    >
                      <DeleteSearchIcon />
                    </button>
                  </div>
                </m.article>
              ))}
            </div>
            {pageCount > 1 && (
              <nav className={styles.pagination} aria-label="History pages">
                <span>
                  Showing {(page - 1) * PAGE_SIZE + 1}–
                  {Math.min(page * PAGE_SIZE, history.data?.total ?? 0)} of{" "}
                  {history.data?.total ?? 0}
                </span>
                <div>
                  <button type="button" disabled={page <= 1} onClick={() => goToPage(page - 1)}>
                    Previous
                  </button>
                  <button
                    type="button"
                    disabled={page >= pageCount}
                    onClick={() => goToPage(page + 1)}
                  >
                    Next
                  </button>
                </div>
              </nav>
            )}
          </>
        )}
      </section>
      <ConfirmDialog
        open={pendingDelete.length > 0}
        onOpenChange={(open) => {
          if (!open) setPendingDelete([]);
        }}
        title={
          pendingDelete.length === 1
            ? "Delete this search?"
            : `Delete ${pendingDelete.length} searches?`
        }
        description="This removes the selected search history permanently. This action cannot be undone."
        busy={remove.isPending}
        onConfirm={() => remove.mutate(pendingDelete)}
      />
    </main>
  );
}
