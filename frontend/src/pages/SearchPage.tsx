import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { searchApi } from "../api/domain";
import { ApiError } from "../api/errors";
import type { SearchFilters, SearchHit, SearchRequest } from "../api/types";
import { queryKeys } from "../app/queryKeys";
import { SearchForm } from "../components/SearchForm";
import { citationFor, formatAuthors, formatDate, parseSearchParameters } from "../lib/format";
import styles from "../styles/app.module.css";

function ResultItem({ hit }: { hit: SearchHit }) {
  const [copied, setCopied] = useState(false);
  const copyCitation = async () => {
    await navigator.clipboard.writeText(citationFor(hit));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  };
  return (
    <article className={styles.resultItem}>
      <h2>
        <a href={hit.arxiv_url} target="_blank" rel="noopener noreferrer">
          {hit.title}
        </a>
      </h2>
      <p className={styles.authors}>{formatAuthors(hit.authors)}</p>
      <dl className={styles.metadata}>
        <div>
          <dt>Year</dt>
          <dd>{new Date(hit.submitted_at).getUTCFullYear()}</dd>
        </div>
        <div>
          <dt>arXiv</dt>
          <dd>{hit.arxiv_id}</dd>
        </div>
        <div>
          <dt>Submitted</dt>
          <dd>{formatDate(hit.submitted_at, "short")}</dd>
        </div>
        <div>
          <dt>Version</dt>
          <dd>v{hit.version}</dd>
        </div>
        <div>
          <dt>Score</dt>
          <dd>{hit.score.toFixed(3)}</dd>
        </div>
      </dl>
      {hit.categories.length > 0 && (
        <p className={styles.categories}>{hit.categories.join(" · ")}</p>
      )}
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
    </article>
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
        />
      </div>
      <div className={styles.readingColumn}>
        <FilterChips filters={parsed.filters} onRemove={removeFilter} />
        {!parsed.query && (
          <div className={styles.state}>
            <h1>Start with a research question</h1>
            <p>Enter a topic, method, or question above.</p>
          </div>
        )}
        {result.isPending && parsed.query && (
          <div className={styles.loadingState} role="status">
            <span />
            Searching the literature…
          </div>
        )}
        {error && (
          <div className={styles.errorState} role="alert">
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
            {error.status === 429 && <Link to="/login">Sign in</Link>}
          </div>
        )}
        {result.data?.degraded && (
          <div className={styles.notice} role="status">
            Some abstracts are temporarily unavailable. The available results are shown below.
          </div>
        )}
        {result.data && result.data.hits.length === 0 && (
          <div className={styles.state}>
            <h1>No papers found</h1>
            <p>Try a broader phrase or a different way of describing the topic.</p>
          </div>
        )}
        {result.data && result.data.hits.length > 0 && (
          <>
            <div className={styles.resultsSummary}>
              <p>
                <strong>{result.data.result_count}</strong> results for “{result.data.query}”
              </p>
              <span>
                {result.data.strength === "thorough" ? "Thorough" : "Standard"} ·{" "}
                {(result.data.elapsed_ms / 1000).toFixed(2)}s
              </span>
            </div>
            <div className={styles.resultList}>
              {result.data.hits.map((hit) => (
                <ResultItem key={`${hit.arxiv_id}-${hit.rank}`} hit={hit} />
              ))}
            </div>
          </>
        )}
      </div>
    </main>
  );
}
