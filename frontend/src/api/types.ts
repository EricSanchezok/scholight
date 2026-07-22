import type { components } from "./schema";

export type ApiSchemas = components["schemas"];
export type SearchRequest = ApiSchemas["PublicSearchRequest"];
export type SearchResponse = ApiSchemas["PublicSearchResponse"];
export type SearchHit = ApiSchemas["PublicSearchHit"];
export type SearchFilters = ApiSchemas["PublicSearchFilters"];
export type SearchStrength = ApiSchemas["SearchStrength"];
export type HistoryItem = ApiSchemas["PublicSearchHistoryItem"];
export type HistoryPage = ApiSchemas["PublicSearchHistoryPage"];
export type UserProfile = ApiSchemas["UserPublic"];
export type QuotaStatus = ApiSchemas["QuotaStatus"];
export type TokenResponse = ApiSchemas["TokenResponse"];
export type LoginRequest = ApiSchemas["LoginRequest"];
export type RegisterRequest = ApiSchemas["RegisterRequest"];
