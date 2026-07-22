import type { SearchFilters, SearchHit, SearchStrength } from "../api/types";
import { routes } from "../app/routes";

export function formatAuthors(authors: string[]): string {
  if (authors.length <= 2) return authors.join(", ");
  return `${authors[0]} et al.`;
}

export function formatDate(value: string, style: "long" | "short" = "long"): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en", {
    year: "numeric",
    month: style === "long" ? "long" : "short",
    day: "numeric",
  }).format(date);
}

export function citationFor(hit: SearchHit): string {
  const authors = hit.authors.join(", ");
  const year = new Date(hit.submitted_at).getUTCFullYear();
  return `${authors} (${year}). ${hit.title}. arXiv:${hit.arxiv_id}. ${hit.arxiv_url}`;
}

export interface SearchParameters {
  query: string;
  strength: SearchStrength;
  filters: SearchFilters;
}

export function parseSearchParameters(params: URLSearchParams): SearchParameters {
  return {
    query: (params.get("q") ?? "").trim().slice(0, 500),
    strength: params.get("strength") === "thorough" ? "thorough" : "standard",
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
