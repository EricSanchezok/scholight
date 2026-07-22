import { useEffect, useState } from "react";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { useNavigate } from "react-router-dom";

import type { SearchFilters, SearchStrength } from "../api/types";
import { buildSearchUrl } from "../lib/format";
import styles from "../styles/app.module.css";
import { EditorialSelect } from "./EditorialSelect";

const strengthOptions = [
  { value: "standard", label: "Standard" },
  { value: "thorough", label: "Thorough" },
] as const;

interface Props {
  initialQuery?: string;
  initialStrength?: SearchStrength;
  filters?: SearchFilters;
  compact?: boolean;
  busy?: boolean;
}

export function SearchForm({
  initialQuery = "",
  initialStrength = "standard",
  filters = {},
  compact = false,
  busy = false,
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
    navigate(
      buildSearchUrl({
        query: normalized,
        strength,
        filters,
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
        maxLength={501}
        aria-describedby={error ? "search-error" : undefined}
      />
      <div className={styles.searchActions}>
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
            <m.span
              key={busy ? "searching" : "search"}
              initial={{ opacity: 0, y: 3 }}
              animate={{ opacity: 1, y: 0, transition: { duration: 0.12 } }}
              exit={{ opacity: 0, y: -2, transition: { duration: 0.08 } }}
            >
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
