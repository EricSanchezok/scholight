import { useQuery } from "@tanstack/react-query";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { searchApi } from "../api/domain";
import { ApiError } from "../api/errors";
import type { SearchFilters, SearchHit, SearchRequest } from "../api/types";
import { queryKeys } from "../app/queryKeys";
import { useAuth } from "../auth/context";
import { SearchForm } from "../components/SearchForm";
import { SearchResultsSkeleton } from "../components/SearchResultsSkeleton";
import { citationFor, formatAuthors, formatDate, parseSearchParameters } from "../lib/format";
import styles from "../styles/app.module.css";

function ResultItem({ hit, index }: { hit: SearchHit; index: number }) {
  const [copied, setCopied] = useState(false);
  const copyCitation = async () => {
    await navigator.clipboard.writeText(citationFor(hit));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  };
  return (
    <m.article
      className={styles.resultItem}
      initial={{ opacity: 0, y: 5 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, delay: Math.min(index * 0.02, 0.18) }}
    >
      <h2>
        <a href={hit.arxiv_url} target="_blank" rel="noopener noreferrer">
          {hit.title}
        </a>
      </h2>
      <p className={styles.authors}>
        {formatAuthors(hit.authors)} · {new Date(hit.submitted_at).getUTCFullYear()} · arXiv:
        {hit.arxiv_id}
      </p>
      <p className={styles.metadata}>
        {hit.categories.join(" · ")} · Submitted {formatDate(hit.submitted_at, "short")} · v
        {hit.version} · Score {hit.score.toFixed(3)}
      </p>
      {hit.abstract && <p className={styles.abstract}>{hit.abstract}</p>}
      <div className={styles.resultActions}>
        <a href={hit.arxiv_url} target="_blank" rel="noopener noreferrer">
          arXiv
        </a>
        <a href={hit.pdf_url} target="_blank" rel="noopener noreferrer">
          PDF
        </a>
        <button type="button" onClick={() => void copyCitation()}>
          Cite
        </button>
        <span aria-live="polite">{copied ? "Citation copied" : ""}</span>
      </div>
    </m.article>
  );
}

function FilterChips({
  filters,
  onRemove,
}: {
  filters: SearchFilters;
  onRemove: (key: keyof SearchFilters, value?: string) => void;
}) {
  const chips: { key: keyof SearchFilters; value?: string; label: string }[] = [
    ...(filters.categories ?? []).map((value) => ({
      key: "categories" as const,
      value,
      label: `Category: ${value}`,
    })),
    ...(filters.authors ?? []).map((value) => ({
      key: "authors" as const,
      value,
      label: `Author: ${value}`,
    })),
    ...(filters.date_from
      ? [{ key: "date_from" as const, label: `From: ${filters.date_from}` }]
      : []),
    ...(filters.date_to ? [{ key: "date_to" as const, label: `To: ${filters.date_to}` }] : []),
  ];
  if (!chips.length) return null;
  return (
    <div className={styles.chips}>
      {chips.map((chip) => (
        <button
          key={`${chip.key}-${chip.value ?? ""}`}
          type="button"
          onClick={() => onRemove(chip.key, chip.value)}
        >
          {chip.label} <span aria-hidden="true">×</span>
        </button>
      ))}
    </div>
  );
}

export function SearchPage() {
  const { status: authStatus } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const parsed = useMemo(() => parseSearchParameters(searchParams), [searchParams]);
  const request: SearchRequest = {
    query: parsed.query,
    strength: parsed.strength,
    limit: 10,
    filters: parsed.filters,
  };
  const result = useQuery({
    queryKey: queryKeys.search(request),
    queryFn: () => searchApi.search(request),
    enabled: Boolean(parsed.query),
    staleTime: 5 * 60 * 1000,
    retry: false,
  });

  useEffect(() => {
    document.title = parsed.query ? `${parsed.query} — Scholight` : "Search — Scholight";
  }, [parsed.query]);

  const removeFilter = (key: keyof SearchFilters, value?: string) => {
    const next = new URLSearchParams(searchParams);
    const names: Record<keyof SearchFilters, string> = {
      categories: "category",
      authors: "author",
      date_from: "from",
      date_to: "to",
    };
    if (value && (key === "categories" || key === "authors")) {
      const values = next.getAll(names[key]).filter((item) => item !== value);
      next.delete(names[key]);
      values.forEach((item) => next.append(names[key], item));
    } else next.delete(names[key]);
    setSearchParams(next);
  };

  const error = result.error instanceof ApiError ? result.error : null;
  return (
    <main className={styles.resultsPage}>
      <div className={styles.resultsSearch}>
        <SearchForm
          initialQuery={parsed.query}
          initialStrength={parsed.strength}
          filters={parsed.filters}
          compact
          busy={result.isPending && Boolean(parsed.query)}
        />
        {error?.status === 422 && (
          <p className={styles.resultsQueryError} role="alert">
            {error.fieldErrors?.[0]?.message ?? error.message}
          </p>
        )}
      </div>
      <div className={styles.readingColumn}>
        <FilterChips filters={parsed.filters} onRemove={removeFilter} />
        {!parsed.query && (
          <div className={styles.state}>
            <h1>Start with a research question</h1>
            <p>Enter a topic, method, or question above.</p>
          </div>
        )}
        <AnimatePresence initial={false} mode="wait">
          {result.isPending && parsed.query ? (
            <m.div key={`loading-${parsed.query}-${parsed.strength}`} exit={{ opacity: 0 }}>
              <SearchResultsSkeleton />
            </m.div>
          ) : error ? (
            <m.div
              className={styles.errorState}
              role="alert"
              key="search-error"
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
            >
              <h1>
                {error.status === 429 ? "Search limit reached" : "We couldn’t complete that search"}
              </h1>
              <p>
                {error.message}
                {error.retryAfter ? ` Try again in about ${error.retryAfter} seconds.` : ""}
              </p>
              <button
                className={styles.secondaryButton}
                type="button"
                onClick={() => void result.refetch()}
              >
                Retry
              </button>
              {error.status === 429 && authStatus === "anonymous" && (
                <Link to="/login">Sign in</Link>
              )}
            </m.div>
          ) : result.data && result.data.hits.length === 0 ? (
            <m.div
              className={styles.state}
              key="search-empty"
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
            >
              <h1>No papers found</h1>
              <p>Try a broader phrase or a different way of describing the topic.</p>
            </m.div>
          ) : result.data && result.data.hits.length > 0 ? (
            <m.div
              key={`results-${parsed.query}-${parsed.strength}`}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1, transition: { duration: 0.14 } }}
              exit={{ opacity: 0, transition: { duration: 0.08 } }}
            >
              <div className={styles.resultsSummary}>
                <h1>Search results</h1>
                <span>
                  {result.data.result_count} papers ·{" "}
                  {result.data.strength === "thorough" ? "Thorough" : "Standard"}
                </span>
              </div>
              <div className={styles.resultList}>
                {result.data.hits.map((hit, index) => (
                  <ResultItem key={`${hit.arxiv_id}-${hit.rank}`} hit={hit} index={index} />
                ))}
              </div>
            </m.div>
          ) : null}
        </AnimatePresence>
        {result.data?.degraded && (
          <div className={styles.notice} role="status">
            Some abstracts are temporarily unavailable. The available results are shown below.
          </div>
        )}
      </div>
    </main>
  );
}
