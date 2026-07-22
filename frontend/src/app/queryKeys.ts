import type { SearchRequest } from "../api/types";

export const queryKeys = {
  search: (request: SearchRequest) => ["search", request] as const,
  history: (q: string, page: number) => ["private", "history", q, page] as const,
  profile: ["private", "profile"] as const,
  quotas: ["private", "quotas"] as const,
  accessKeys: ["private", "access-keys"] as const,
  usageSummary: ["private", "usage", "summary"] as const,
  usageVolume: ["private", "usage", "volume", "30-days"] as const,
  usageLatency: ["private", "usage", "latency", "30-days"] as const,
  usageRecords: ["private", "usage", "records"] as const,
  sessions: ["private", "sessions"] as const,
};
