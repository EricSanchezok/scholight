import { useQuery } from "@tanstack/react-query";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { searchApi } from "../api/domain";
import { ApiError } from "../api/errors";
import type { SearchFilters, SearchHit, SearchRequest } from "../api/types";
import { queryKeys } from "../app/queryKeys";
import { routes } from "../app/routes";
import { resultRowMotion, resultsRevealMotion, sectionRevealMotion } from "../app/motion";
import { useAuth } from "../auth/context";
import { productConfig } from "../config/product";
import { SearchForm } from "../components/SearchForm";
import { SearchResultsSkeleton } from "../components/SearchResultsSkeleton";
import {
  citationFor,
  countSearchFilterGroups,
  parseSearchParameters,
  searchResultBylineParts,
  searchResultMetadataParts,
} from "../lib/format";
import { useI18n } from "../i18n/I18nProvider";
import { styles } from "../styles/classes";

function ResultItem({ hit, index }: { hit: SearchHit; index: number }) {
  const { locale, messages } = useI18n();
  const [copied, setCopied] = useState(false);
  const byline = searchResultBylineParts(hit);
  const metadata = searchResultMetadataParts(hit, locale, {
    submitted: messages.search.submitted,
    score: messages.search.score,
  });
  const copyCitation = async () => {
    await navigator.clipboard.writeText(citationFor(hit));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  };
  return (
    <m.article className={styles.resultItem} {...resultRowMotion(index)}>
      <h2>
        <a href={hit.arxiv_url} target="_blank" rel="noopener noreferrer">
          {hit.title}
        </a>
      </h2>
      <p className={styles.authors}>{byline.join(" · ")}</p>
      <p className={styles.metadata}>{metadata.join(" · ")}</p>
      {hit.abstract && <p className={styles.abstract}>{hit.abstract}</p>}
      <div className={styles.resultActions}>
        <a href={hit.arxiv_url} target="_blank" rel="noopener noreferrer">
          arXiv
        </a>
        <a href={hit.pdf_url} target="_blank" rel="noopener noreferrer">
          PDF
        </a>
        <button type="button" onClick={() => void copyCitation()}>
          {messages.search.cite}
        </button>
        <span aria-live="polite">{copied ? messages.search.citationCopied : ""}</span>
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
  const { messages } = useI18n();
  const { status: authStatus } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const parsed = useMemo(() => parseSearchParameters(searchParams), [searchParams]);
  const request: SearchRequest = {
    query: parsed.query,
    strength: parsed.strength,
    limit: parsed.limit,
    filters: parsed.filters,
  };
  const result = useQuery({
    queryKey: queryKeys.search(request),
    queryFn: () => searchApi.search(request),
    enabled: Boolean(parsed.query),
    staleTime: productConfig.search.cacheTimeMs,
    retry: false,
  });

  useEffect(() => {
    document.title = messages.titles.search(parsed.query || undefined);
  }, [messages.titles, parsed.query]);

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
  const filterCount = countSearchFilterGroups(parsed.filters);
  return (
    <main className={styles.resultsPage}>
      <div className={styles.resultsSearch}>
        <SearchForm
          initialQuery={parsed.query}
          initialStrength={parsed.strength}
          initialLimit={parsed.limit}
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
            <h1>{messages.search.startTitle}</h1>
            <p>{messages.search.startHint}</p>
          </div>
        )}
        <AnimatePresence initial={false} mode="popLayout">
          {result.isPending && parsed.query ? (
            <m.div key={`loading-${parsed.query}-${parsed.strength}`} exit={{ opacity: 0 }}>
              <SearchResultsSkeleton />
            </m.div>
          ) : error ? (
            <m.div
              className={styles.errorState}
              role="alert"
              key="search-error"
              {...sectionRevealMotion}
            >
              <h1>
                {error.status === 429 ? messages.search.limitReached : messages.search.failedTitle}
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
                {messages.common.retry}
              </button>
              {error.status === 429 && authStatus === "anonymous" && (
                <Link to={routes.login.path}>{messages.search.signIn}</Link>
              )}
            </m.div>
          ) : result.data && result.data.hits.length === 0 ? (
            <m.div className={styles.state} key="search-empty" {...sectionRevealMotion}>
              <h1>{messages.search.noPapers}</h1>
              <p>{messages.search.noPapersHint}</p>
            </m.div>
          ) : result.data && result.data.hits.length > 0 ? (
            <m.div key={`results-${parsed.query}-${parsed.strength}`} {...resultsRevealMotion}>
              <div className={styles.resultsSummary}>
                <h1>{messages.search.resultsTitle}</h1>
                <span>
                  {result.data.result_count} papers ·{" "}
                  {result.data.strength === "thorough"
                    ? messages.search.thorough
                    : messages.search.standard}
                  {filterCount ? ` · ${filterCount} filters` : ""}
                </span>
              </div>
              <div>
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
