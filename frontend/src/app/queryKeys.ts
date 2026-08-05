import type { SearchRequest } from "../api/types";
import { productConfig } from "../config/product";

const privateRoot = ["private"] as const;
const historyRoot = [...privateRoot, "history"] as const;
const surveyRoot = [...privateRoot, "survey"] as const;

export const queryKeys = {
  capabilities: ["public", "capabilities"] as const,
  search: (request: SearchRequest) => ["search", request] as const,
  privateRoot,
  historyRoot,
  history: (q: string, page: number) => [...historyRoot, q, page] as const,
  surveyRoot,
  surveys: (view: "active" | "completed" | "all") => [...surveyRoot, "list", view] as const,
  survey: (surveyId: string) => [...surveyRoot, surveyId] as const,
  surveyProgress: (surveyId: string) => [...surveyRoot, surveyId, "progress"] as const,
  surveyDrafts: (surveyId: string) => [...surveyRoot, surveyId, "drafts"] as const,
  surveyReport: (surveyId: string) => [...surveyRoot, surveyId, "report"] as const,
  surveyArtifacts: (surveyId: string) => [...surveyRoot, surveyId, "artifacts"] as const,
  profile: [...privateRoot, "profile"] as const,
  avatar: [...privateRoot, "avatar"] as const,
  quotas: [...privateRoot, "quotas"] as const,
  accessKeys: [...privateRoot, "access-keys"] as const,
  usageSummary: [...privateRoot, "usage", "summary"] as const,
  usageVolume: [...privateRoot, "usage", "volume", productConfig.usage.rangeDays] as const,
  usageLatency: [...privateRoot, "usage", "latency", productConfig.usage.rangeDays] as const,
  usageRecords: [...privateRoot, "usage", "records"] as const,
  sessions: [...privateRoot, "sessions"] as const,
  adminAudit: [...privateRoot, "admin", "audit"] as const,
  adminAnalytics: (days: number) => [...privateRoot, "admin", "analytics", days] as const,
  adminOperations: (days: number, issueLimit: number) =>
    [...privateRoot, "admin", "operations", days, issueLimit] as const,
};
