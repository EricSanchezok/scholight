import type { SearchFilters, SearchHit, SearchStrength } from "../api/types";
import { routes } from "../app/routes";
import { formatResearchDate } from "../i18n/format";
import type { AppLocale } from "../i18n/I18nProvider";

export function formatAuthors(authors: string[]): string {
  if (authors.length === 0) return "Unknown authors";
  if (authors.length <= 2) return authors.join(", ");
  return `${authors[0]} et al.`;
}

function validDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatDate(
  value: string,
  style: "long" | "short" = "long",
  locale?: AppLocale,
): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return formatResearchDate(date, style, locale);
}

export function citationFor(hit: SearchHit): string {
  const authors = hit.authors.length > 0 ? hit.authors.join(", ") : "Unknown authors";
  const submitted = validDate(hit.submitted_at);
  const year = submitted?.getUTCFullYear() ?? "n.d.";
  return `${authors} (${year}). ${hit.title}. arXiv:${hit.arxiv_id}. ${hit.arxiv_url}`;
}

export function searchResultBylineParts(hit: SearchHit): string[] {
  const parts = [formatAuthors(hit.authors)];
  const submitted = validDate(hit.submitted_at);
  if (submitted !== null) parts.push(String(submitted.getUTCFullYear()));
  parts.push(`arXiv:${hit.arxiv_id}`);
  return parts;
}

export function searchResultMetadataParts(
  hit: SearchHit,
  locale: AppLocale | undefined,
  labels: { submitted: string; score: string },
): string[] {
  const parts: string[] = [];
  if (hit.categories.length > 0) parts.push(hit.categories.join(" · "));
  if (validDate(hit.submitted_at) !== null && hit.submitted_at !== null) {
    parts.push(`${labels.submitted} ${formatDate(hit.submitted_at, "short", locale)}`);
  }
  if (hit.version !== null) parts.push(`v${hit.version}`);
  parts.push(`${labels.score} ${hit.score.toFixed(3)}`);
  return parts;
}

export interface SearchParameters {
  query: string;
  strength: SearchStrength;
  limit: number;
  filters: SearchFilters;
}

export type DatePreset = "any" | "1month" | "3months" | "6months" | "12months";

const datePresetMonths: Record<Exclude<DatePreset, "any">, number> = {
  "1month": 1,
  "3months": 3,
  "6months": 6,
  "12months": 12,
};

export function dateFromPreset(preset: Exclude<DatePreset, "any">, now = new Date()): string {
  const months = datePresetMonths[preset];
  const targetMonth = now.getUTCMonth() - months;
  const firstOfTargetMonth = new Date(Date.UTC(now.getUTCFullYear(), targetMonth, 1));
  const finalDayOfTargetMonth = new Date(
    Date.UTC(firstOfTargetMonth.getUTCFullYear(), firstOfTargetMonth.getUTCMonth() + 1, 0),
  ).getUTCDate();
  const target = new Date(
    Date.UTC(
      firstOfTargetMonth.getUTCFullYear(),
      firstOfTargetMonth.getUTCMonth(),
      Math.min(now.getUTCDate(), finalDayOfTargetMonth),
    ),
  );
  return target.toISOString().slice(0, 10);
}

export function countSearchFilterGroups(filters: SearchFilters): number {
  return (
    Number(Boolean(filters.categories?.length)) +
    Number(Boolean(filters.authors?.length)) +
    Number(Boolean(filters.date_from || filters.date_to))
  );
}

export function parseSearchParameters(params: URLSearchParams): SearchParameters {
  const requestedLimit = Number(params.get("limit") ?? 10);
  const limit = [10, 20, 30, 40, 50].includes(requestedLimit) ? requestedLimit : 10;
  return {
    query: (params.get("q") ?? "").trim().slice(0, 500),
    strength: params.get("strength") === "thorough" ? "thorough" : "standard",
    limit,
    filters: {
      categories: params.getAll("category").filter(Boolean),
      authors: params.getAll("author").filter(Boolean),
      date_from: params.get("from"),
      date_to: params.get("to"),
    },
  };
}

export function buildSearchUrl(parameters: SearchParameters): string {
  const params = new URLSearchParams({ q: parameters.query, strength: parameters.strength });
  if (parameters.limit !== 10) params.set("limit", String(parameters.limit));
  parameters.filters.categories?.forEach((value) => params.append("category", value));
  parameters.filters.authors?.forEach((value) => params.append("author", value));
  if (parameters.filters.date_from) params.set("from", parameters.filters.date_from);
  if (parameters.filters.date_to) params.set("to", parameters.filters.date_to);
  return `${routes.search.path}?${params.toString()}`;
}

export function avatarInitials(displayName: string | null | undefined, email: string): string {
  const value = displayName?.trim() || email.split("@")[0] || "S";
  const words = value.split(/\s+/).filter(Boolean);
  const first = words[0]?.[0] ?? "S";
  const last = words.at(-1)?.[0] ?? "";
  return (words.length > 1 ? `${first}${last}` : value.slice(0, 2)).toUpperCase();
}
