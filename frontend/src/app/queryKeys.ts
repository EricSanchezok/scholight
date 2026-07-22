import type { SearchRequest } from "../api/types";

export const queryKeys = {
  search: (request: SearchRequest) => ["search", request] as const,
  history: (q: string, page: number) => ["private", "history", q, page] as const,
  profile: ["private", "profile"] as const,
  quotas: ["private", "quotas"] as const,
};
