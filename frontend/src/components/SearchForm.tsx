import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import type { SearchFilters, SearchStrength } from "../api/types";
import { buildSearchUrl } from "../lib/format";
import styles from "../styles/app.module.css";
import { ChevronDownIcon } from "./icons";

function filterValues(value: string): string[] {
  return [
    ...new Set(
      value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ].slice(0, 10);
}

interface Props {
  initialQuery?: string;
  initialStrength?: SearchStrength;
  filters?: SearchFilters;
  compact?: boolean;
}

export function SearchForm({
  initialQuery = "",
  initialStrength = "standard",
  filters = {},
  compact = false,
}: Props) {
  const navigate = useNavigate();
  const [query, setQuery] = useState(initialQuery);
  const [strength, setStrength] = useState<SearchStrength>(initialStrength);
  const [error, setError] = useState("");
  const [filtersOpen, setFiltersOpen] = useState(
    Boolean(
      filters.categories?.length || filters.authors?.length || filters.date_from || filters.date_to,
    ),
  );
  const [categories, setCategories] = useState((filters.categories ?? []).join(", "));
  const [authors, setAuthors] = useState((filters.authors ?? []).join(", "));
  const [dateFrom, setDateFrom] = useState(filters.date_from ?? "");
  const [dateTo, setDateTo] = useState(filters.date_to ?? "");

  useEffect(() => setQuery(initialQuery), [initialQuery]);
  useEffect(() => setStrength(initialStrength), [initialStrength]);
  useEffect(() => setCategories((filters.categories ?? []).join(", ")), [filters.categories]);
  useEffect(() => setAuthors((filters.authors ?? []).join(", ")), [filters.authors]);
  useEffect(() => setDateFrom(filters.date_from ?? ""), [filters.date_from]);
  useEffect(() => setDateTo(filters.date_to ?? ""), [filters.date_to]);

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const normalized = query.trim();
    if (!normalized) return setError("Enter a research question or topic.");
    if (normalized.length > 500) return setError("Keep your query to 500 characters or fewer.");
    if (dateFrom && dateTo && dateFrom > dateTo) {
      return setError("The start date must be before the end date.");
    }
    setError("");
    navigate(
      buildSearchUrl({
        query: normalized,
        strength,
        filters: {
          categories: filterValues(categories),
          authors: filterValues(authors),
          date_from: dateFrom || null,
          date_to: dateTo || null,
        },
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
        placeholder="Search papers, methods, or research questions"
        maxLength={501}
        aria-describedby={error ? "search-error" : undefined}
      />
      <div className={styles.searchActions}>
        <label className={styles.strengthSelect}>
          <span className="sr-only">Search strength</span>
          <select
            value={strength}
            onChange={(event) => setStrength(event.target.value as SearchStrength)}
          >
            <option value="standard">Standard</option>
            <option value="thorough">Thorough</option>
          </select>
          <ChevronDownIcon />
        </label>
        <button
          className={styles.filterToggle}
          type="button"
          aria-expanded={filtersOpen}
          aria-controls={compact ? "result-filters" : "home-filters"}
          onClick={() => setFiltersOpen((value) => !value)}
        >
          Filters
        </button>
        <button className={styles.primaryButton} type="submit">
          Search
        </button>
      </div>
      {filtersOpen && (
        <div className={styles.searchFilters} id={compact ? "result-filters" : "home-filters"}>
          <label>
            Categories
            <input
              value={categories}
              onChange={(event) => setCategories(event.target.value)}
              placeholder="cs.AI, cs.LG"
            />
          </label>
          <label>
            Authors
            <input
              value={authors}
              onChange={(event) => setAuthors(event.target.value)}
              placeholder="Ada Lovelace"
            />
          </label>
          <label>
            From date
            <input
              type="date"
              value={dateFrom}
              onChange={(event) => setDateFrom(event.target.value)}
            />
          </label>
          <label>
            To date
            <input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} />
          </label>
        </div>
      )}
      {error && (
        <p className={styles.fieldError} id="search-error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
