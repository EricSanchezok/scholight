import type { SearchRequest } from "../api/types";
import { productConfig } from "../config/product";

const privateRoot = ["private"] as const;
const historyRoot = [...privateRoot, "history"] as const;

export const queryKeys = {
  search: (request: SearchRequest) => ["search", request] as const,
  privateRoot,
  historyRoot,
  history: (q: string, page: number) => [...historyRoot, q, page] as const,
  profile: [...privateRoot, "profile"] as const,
  quotas: [...privateRoot, "quotas"] as const,
  accessKeys: [...privateRoot, "access-keys"] as const,
  usageSummary: [...privateRoot, "usage", "summary"] as const,
  usageVolume: [...privateRoot, "usage", "volume", productConfig.usage.rangeDays] as const,
  usageLatency: [...privateRoot, "usage", "latency", productConfig.usage.rangeDays] as const,
  usageRecords: [...privateRoot, "usage", "records"] as const,
  sessions: [...privateRoot, "sessions"] as const,
  adminAudit: [...privateRoot, "admin", "audit"] as const,
};
