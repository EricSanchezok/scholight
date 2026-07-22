import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import type { SearchFilters, SearchStrength } from "../api/types";
import { buildSearchUrl } from "../lib/format";
import styles from "../styles/app.module.css";
import { ChevronDownIcon } from "./icons";

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

  useEffect(() => setQuery(initialQuery), [initialQuery]);
  useEffect(() => setStrength(initialStrength), [initialStrength]);

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const normalized = query.trim();
    if (!normalized) return setError("Enter a research question or topic.");
    if (normalized.length > 500) return setError("Keep your query to 500 characters or fewer.");
    setError("");
    navigate(buildSearchUrl({ query: normalized, strength, filters }));
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
        <button className={styles.primaryButton} type="submit">
          Search
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
