import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { useEffect, useMemo, useRef, useState } from "react";

import type { SearchFilters } from "../api/types";
import { chevronMotion, popoverMotion } from "../app/motion";
import { countSearchFilterGroups, dateFromPreset, type DatePreset } from "../lib/format";
import { styles } from "../styles/classes";
import { ChevronDownIcon } from "./icons";
import {
  authorSummary,
  copySearchFilters,
  dateOptions,
  inferDatePreset,
  resultLimits,
  subjectOptions,
  subjectSummary,
} from "./searchFilters";

type Picker = "subject" | "date" | "author" | null;

export function SearchFiltersControl({
  filters,
  limit,
  onApply,
}: {
  filters: SearchFilters;
  limit: number;
  onApply: (filters: SearchFilters, limit: number) => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [picker, setPicker] = useState<Picker>(null);
  const [draftFilters, setDraftFilters] = useState<SearchFilters>(() => copySearchFilters(filters));
  const [draftLimit, setDraftLimit] = useState(limit);
  const [subjectQuery, setSubjectQuery] = useState("");
  const [authorQuery, setAuthorQuery] = useState("");
  const filterCount = countSearchFilterGroups(filters);
  const selectedDatePreset = inferDatePreset(draftFilters);

  useEffect(() => {
    if (open) return;
    setDraftFilters(copySearchFilters(filters));
    setDraftLimit(limit);
  }, [filters, limit, open]);

  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
        setPicker(null);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (picker) setPicker(null);
      else setOpen(false);
    };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open, picker]);

  const filteredSubjects = useMemo(() => {
    const query = subjectQuery.trim().toLocaleLowerCase();
    if (!query) return subjectOptions;
    return subjectOptions.filter((option) =>
      `${option.label} ${option.value}`.toLocaleLowerCase().includes(query),
    );
  }, [subjectQuery]);

  const toggleOpen = () => {
    if (!open) {
      setDraftFilters(copySearchFilters(filters));
      setDraftLimit(limit);
    }
    setOpen((current) => !current);
    setPicker(null);
  };

  const toggleSubject = (value: string) => {
    setDraftFilters((current) => {
      const selected = new Set(current.categories ?? []);
      if (selected.has(value)) selected.delete(value);
      else if (selected.size < 10) selected.add(value);
      return { ...current, categories: [...selected] };
    });
  };

  const selectDate = (preset: DatePreset) => {
    setDraftFilters((current) => {
      const next = copySearchFilters(current);
      delete next.date_from;
      delete next.date_to;
      if (preset !== "any") next.date_from = dateFromPreset(preset);
      return next;
    });
    setPicker(null);
  };

  const addAuthor = () => {
    const author = authorQuery.trim();
    if (!author) return;
    setDraftFilters((current) => {
      const authors = current.authors ?? [];
      if (authors.includes(author) || authors.length >= 10) return current;
      return { ...current, authors: [...authors, author] };
    });
    setAuthorQuery("");
  };

  const toggleAuthor = (value: string) => {
    setDraftFilters((current) => ({
      ...current,
      authors: (current.authors ?? []).filter((author) => author !== value),
    }));
  };

  const clearAll = () => {
    setDraftFilters({ categories: [], authors: [] });
    setDraftLimit(10);
    setSubjectQuery("");
    setAuthorQuery("");
    setPicker(null);
  };

  const apply = () => {
    onApply(copySearchFilters(draftFilters), draftLimit);
    setOpen(false);
    setPicker(null);
  };

  const dateLabel =
    selectedDatePreset === "custom"
      ? "Date filter applied"
      : (dateOptions.find((option) => option.value === selectedDatePreset)?.label ?? "Any time");

  return (
    <div className={styles.filtersControl} ref={rootRef}>
      <button
        className={styles.filterTrigger}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={toggleOpen}
      >
        <span>{filterCount ? `Filters · ${filterCount}` : "Filters"}</span>
        <m.span className={styles.selectChevron} {...chevronMotion(open)}>
          <ChevronDownIcon />
        </m.span>
      </button>
      <AnimatePresence>
        {open && (
          <m.div
            className={styles.filtersPopover}
            role="dialog"
            aria-label="Refine search"
            {...popoverMotion}
          >
            <h2 className={styles.filtersTitle}>Refine search</h2>

            <div className={styles.filterField}>
              <span className={styles.filterLabel}>Subject</span>
              <button
                className={styles.filterFieldTrigger}
                type="button"
                aria-label="Subject"
                aria-expanded={picker === "subject"}
                onClick={() => setPicker((current) => (current === "subject" ? null : "subject"))}
              >
                <span>{subjectSummary(draftFilters.categories)}</span>
                <m.span className={styles.selectChevron} {...chevronMotion(picker === "subject")}>
                  <ChevronDownIcon />
                </m.span>
              </button>
              <AnimatePresence>
                {picker === "subject" && (
                  <m.div className={styles.filterPicker} {...popoverMotion}>
                    <label className="sr-only" htmlFor="subject-filter-search">
                      Find a subject
                    </label>
                    <input
                      className={styles.filterSearch}
                      id="subject-filter-search"
                      value={subjectQuery}
                      onChange={(event) => setSubjectQuery(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") event.preventDefault();
                      }}
                      placeholder="Find a subject"
                    />
                    <div className={styles.filterOptions}>
                      <button
                        className={`${styles.filterOption} ${
                          !draftFilters.categories?.length ? styles.filterOptionSelected : ""
                        }`}
                        type="button"
                        onClick={() =>
                          setDraftFilters((current) => ({ ...current, categories: [] }))
                        }
                      >
                        Any subject
                      </button>
                      {filteredSubjects.map((option) => {
                        const checked = draftFilters.categories?.includes(option.value) ?? false;
                        return (
                          <label
                            className={`${styles.filterOption} ${
                              checked ? styles.filterOptionSelected : ""
                            }`}
                            key={option.value}
                          >
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={() => toggleSubject(option.value)}
                            />
                            <span>{option.label}</span>
                          </label>
                        );
                      })}
                      {!filteredSubjects.length && (
                        <p className={styles.filterEmpty}>No matching subjects.</p>
                      )}
                    </div>
                  </m.div>
                )}
              </AnimatePresence>
            </div>

            <div className={styles.filterField}>
              <span className={styles.filterLabel}>Publication date</span>
              <button
                className={styles.filterFieldTrigger}
                type="button"
                aria-label="Publication date"
                aria-expanded={picker === "date"}
                onClick={() => setPicker((current) => (current === "date" ? null : "date"))}
              >
                <span>{dateLabel}</span>
                <m.span className={styles.selectChevron} {...chevronMotion(picker === "date")}>
                  <ChevronDownIcon />
                </m.span>
              </button>
              <AnimatePresence>
                {picker === "date" && (
                  <m.div className={styles.filterPicker} {...popoverMotion}>
                    <div className={styles.filterOptions}>
                      {dateOptions.map((option) => (
                        <button
                          className={`${styles.filterOption} ${
                            selectedDatePreset === option.value ? styles.filterOptionSelected : ""
                          }`}
                          type="button"
                          key={option.value}
                          aria-pressed={selectedDatePreset === option.value}
                          onClick={() => selectDate(option.value)}
                        >
                          {option.label}
                        </button>
                      ))}
                    </div>
                  </m.div>
                )}
              </AnimatePresence>
            </div>

            <div className={styles.filterField}>
              <label className={styles.filterLabel} htmlFor="author-filter-input">
                Author
              </label>
              <input
                className={`${styles.authorInput} ${
                  picker === "author" ? styles.filterFieldActive : ""
                }`}
                id="author-filter-input"
                aria-label="Author name"
                value={authorQuery}
                onFocus={() => setPicker("author")}
                onChange={(event) => setAuthorQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key !== "Enter") return;
                  event.preventDefault();
                  addAuthor();
                }}
                placeholder={
                  picker === "author" ? "Type an author name" : authorSummary(draftFilters.authors)
                }
              />
              <AnimatePresence>
                {picker === "author" && (
                  <m.div className={styles.filterPicker} {...popoverMotion}>
                    <div className={styles.filterOptions}>
                      {authorQuery.trim() &&
                        !draftFilters.authors?.includes(authorQuery.trim()) && (
                          <button
                            className={`${styles.filterOption} ${styles.authorAddOption}`}
                            type="button"
                            onClick={addAuthor}
                          >
                            Add “{authorQuery.trim()}”
                          </button>
                        )}
                      {(draftFilters.authors ?? []).map((author) => (
                        <label
                          className={`${styles.filterOption} ${styles.filterOptionSelected}`}
                          key={author}
                        >
                          <input type="checkbox" checked onChange={() => toggleAuthor(author)} />
                          <span>{author}</span>
                        </label>
                      ))}
                      {!authorQuery.trim() && !draftFilters.authors?.length && (
                        <p className={styles.filterEmpty}>Type a name, then press Enter.</p>
                      )}
                    </div>
                  </m.div>
                )}
              </AnimatePresence>
            </div>

            <fieldset className={styles.resultsField}>
              <legend className={styles.filterLabel}>Results</legend>
              <div className={styles.resultsControl}>
                {resultLimits.map((value) => (
                  <button
                    className={`${styles.resultsOption} ${
                      draftLimit === value ? styles.resultsOptionSelected : ""
                    }`}
                    type="button"
                    aria-label={`${value} results`}
                    aria-pressed={draftLimit === value}
                    key={value}
                    onClick={() => setDraftLimit(value)}
                  >
                    {value}
                  </button>
                ))}
              </div>
            </fieldset>

            <div className={styles.filterActions}>
              <button className={styles.filterClear} type="button" onClick={clearAll}>
                Clear all
              </button>
              <button
                className={styles.filterApply}
                type="button"
                aria-label="Apply filters"
                onClick={apply}
              >
                Apply
              </button>
            </div>
          </m.div>
        )}
      </AnimatePresence>
    </div>
  );
}
