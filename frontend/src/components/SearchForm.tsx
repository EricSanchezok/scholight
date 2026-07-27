import { useEffect, useState } from "react";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { useNavigate } from "react-router-dom";

import type { SearchFilters, SearchStrength } from "../api/types";
import { buttonLabelMotion } from "../app/motion";
import { productConfig } from "../config/product";
import { buildSearchUrl } from "../lib/format";
import { styles } from "../styles/classes";
import { EditorialSelect } from "./EditorialSelect";
import { SearchFiltersControl } from "./SearchFiltersControl";

const strengthOptions = [
  { value: "standard", label: "Standard" },
  { value: "thorough", label: "Thorough" },
] as const;

const emptyFilters: SearchFilters = {};

interface Props {
  initialQuery?: string;
  initialStrength?: SearchStrength;
  initialLimit?: number;
  filters?: SearchFilters;
  compact?: boolean;
  busy?: boolean;
}

export function SearchForm({
  initialQuery = "",
  initialStrength = "standard",
  initialLimit = productConfig.search.resultLimit,
  filters = emptyFilters,
  compact = false,
  busy = false,
}: Props) {
  const navigate = useNavigate();
  const [query, setQuery] = useState(initialQuery);
  const [strength, setStrength] = useState<SearchStrength>(initialStrength);
  const [searchFilters, setSearchFilters] = useState<SearchFilters>(filters);
  const [limit, setLimit] = useState(initialLimit);
  const [error, setError] = useState("");

  useEffect(() => setQuery(initialQuery), [initialQuery]);
  useEffect(() => setStrength(initialStrength), [initialStrength]);
  useEffect(() => setSearchFilters(filters), [filters]);
  useEffect(() => setLimit(initialLimit), [initialLimit]);

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const normalized = query.trim();
    if (!normalized) return setError("Enter a research question or topic.");
    if (normalized.length > productConfig.search.maxQueryLength)
      return setError("Keep your query to 500 characters or fewer.");
    setError("");
    navigate(
      buildSearchUrl({
        query: normalized,
        strength,
        limit,
        filters: searchFilters,
      }),
    );
  };

  return (
    <form
      className={`${styles.searchForm} ${compact ? styles.searchFormCompact : ""}`}
      onSubmit={submit}
      role="search"
      noValidate
    >
      <label className="sr-only" htmlFor={compact ? "result-query" : "home-query"}>
        Search research papers
      </label>
      <input
        id={compact ? "result-query" : "home-query"}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="Search papers, topics, methods, or questions"
        maxLength={productConfig.search.maxQueryLength + 1}
        aria-describedby={error ? "search-error" : undefined}
      />
      <div className={styles.searchActions}>
        <SearchFiltersControl
          filters={searchFilters}
          limit={limit}
          onApply={(nextFilters, nextLimit) => {
            setSearchFilters(nextFilters);
            setLimit(nextLimit);
            const normalized = query.trim();
            if (compact && normalized) {
              navigate(
                buildSearchUrl({
                  query: normalized,
                  strength,
                  limit: nextLimit,
                  filters: nextFilters,
                }),
              );
            }
          }}
        />
        <div className={styles.strengthSelect}>
          <EditorialSelect
            label="Search strength"
            value={strength}
            options={strengthOptions}
            onValueChange={setStrength}
            variant="strength"
          />
        </div>
        <button className={styles.primaryButton} type="submit" disabled={busy} aria-busy={busy}>
          <AnimatePresence initial={false} mode="popLayout">
            <m.span key={busy ? "searching" : "search"} {...buttonLabelMotion}>
              {busy ? "Searching…" : "Search"}
            </m.span>
          </AnimatePresence>
        </button>
      </div>
      {error && (
        <p className={styles.fieldError} id="search-error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
