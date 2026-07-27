import type { SearchFilters } from "../api/types";
import { dateFromPreset, type DatePreset } from "../lib/format";
import { subjectOptions } from "./arxivSubjects";

export { subjectOptions };

export const dateOptions: readonly { value: DatePreset; label: string }[] = [
  { value: "any", label: "Any time" },
  { value: "1month", label: "Past 1 month" },
  { value: "3months", label: "Past 3 months" },
  { value: "6months", label: "Past 6 months" },
  { value: "12months", label: "Past 12 months" },
];

export const resultLimits = [10, 20, 30, 40, 50] as const;

export function copySearchFilters(filters: SearchFilters): SearchFilters {
  return {
    ...filters,
    categories: [...(filters.categories ?? [])],
    authors: [...(filters.authors ?? [])],
  };
}

export function inferDatePreset(filters: SearchFilters): DatePreset | "custom" {
  if (!filters.date_from && !filters.date_to) return "any";
  if (filters.date_to) return "custom";
  const matching = dateOptions.find(
    (option) => option.value !== "any" && dateFromPreset(option.value) === filters.date_from,
  );
  return matching?.value ?? "custom";
}

export function subjectSummary(categories: string[] | undefined): string {
  if (!categories?.length) return "Any subject";
  if (categories.length === 1) {
    const category = categories[0]!;
    return subjectOptions.find((option) => option.value === category)?.label ?? category;
  }
  return `${categories.length} subjects selected`;
}

export function authorSummary(authors: string[] | undefined): string {
  if (!authors?.length) return "Any author";
  if (authors.length === 1) return authors[0]!;
  return `${authors.length} authors selected`;
}
