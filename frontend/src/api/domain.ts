import { toApiError } from "./errors";
import { apiClient, withAuthRetry, type ApiResult } from "./client";
import type {
  HistoryPage,
  LoginRequest,
  RegisterRequest,
  AccessKey,
  CreateAccessKeyRequest,
  CreatedAccessKey,
  SearchRequest,
  SearchResponse,
  Session,
  AccessTokenResponse,
  UpdateAccessKeyRequest,
  UserProfile,
  UsageLatency,
  UsageRecords,
  UsageSummary,
  UsageVolume,
  AdminCapabilities,
  AdminAnalytics,
  AdminOperations,
  AdminUserLookup,
  AdminAuditEvent,
  QuotaOverrideRequest,
  QuotaOverrideUpdate,
  Survey,
  SurveyActionRequest,
  SurveyArtifacts,
  SurveyCreateRequest,
  SurveyDraft,
  SurveyDraftRequest,
  SurveyList,
  SurveyManualDraftRequest,
  SurveyProgress,
  SurveyView,
  PublicCapabilities,
  AvatarView,
} from "./types";
import { productConfig } from "../config/product";

async function unwrap<T>(promise: Promise<ApiResult<T>>): Promise<T> {
  const result = await promise;
  if (result.data !== undefined) return result.data;
  throw await toApiError(result.response, result.error);
}

async function unwrapEmpty(promise: Promise<ApiResult<undefined>>): Promise<void> {
  const result = await promise;
  if (result.response.ok) return;
  throw await toApiError(result.response, result.error);
}

export const authApi = {
  login: (body: LoginRequest) =>
    unwrap<AccessTokenResponse>(apiClient.POST("/auth/login", { body })),
  register: (body: RegisterRequest) => unwrap(apiClient.POST("/auth/register", { body })),
  resendVerification: (email: string) =>
    unwrap(apiClient.POST("/auth/resend-verification", { body: { email } })),
  verifyEmail: (token: string) => unwrap(apiClient.POST("/auth/verify-email", { body: { token } })),
  forgotPassword: (email: string) =>
    unwrap(apiClient.POST("/auth/forgot-password", { body: { email } })),
  resetPassword: (token: string, newPassword: string) =>
    unwrap(
      apiClient.POST("/auth/reset-password", {
        body: { token, new_password: newPassword },
      }),
    ),
  logout: () => unwrap(withAuthRetry(() => apiClient.POST("/auth/logout"), "protected")),
};

export const searchApi = {
  search: (body: SearchRequest) =>
    unwrap<SearchResponse>(withAuthRetry(() => apiClient.POST("/search", { body }))),
};

export const capabilitiesApi = {
  get: () => unwrap<PublicCapabilities>(apiClient.GET("/capabilities")),
};

export const accountApi = {
  avatar: () => unwrap<AvatarView>(withAuthRetry(() => apiClient.GET("/user/avatar"), "protected")),
  profile: () =>
    unwrap<UserProfile>(withAuthRetry(() => apiClient.GET("/user/profile"), "protected")),
  sessions: () =>
    unwrap<Session[]>(withAuthRetry(() => apiClient.GET("/auth/sessions"), "protected")),
  revokeSession: (sessionId: number) =>
    unwrap(
      withAuthRetry(
        () =>
          apiClient.DELETE("/auth/sessions/{session_id}", {
            params: { path: { session_id: sessionId } },
          }),
        "protected",
      ),
    ),
  revokeOtherSessions: () =>
    unwrap(withAuthRetry(() => apiClient.POST("/auth/sessions/revoke-others"), "protected")),
};

export const accessKeyApi = {
  list: () =>
    unwrap<AccessKey[]>(withAuthRetry(() => apiClient.GET("/user/access-keys"), "protected")),
  create: (body: CreateAccessKeyRequest) =>
    unwrap<CreatedAccessKey>(
      withAuthRetry(() => apiClient.POST("/user/access-keys", { body }), "protected"),
    ),
  update: (keyId: string, body: UpdateAccessKeyRequest) =>
    unwrap<AccessKey>(
      withAuthRetry(
        () =>
          apiClient.PATCH("/user/access-keys/{key_id}", {
            params: { path: { key_id: keyId } },
            body,
          }),
        "protected",
      ),
    ),
  revoke: (keyId: string) =>
    unwrapEmpty(
      withAuthRetry(
        () =>
          apiClient.DELETE("/user/access-keys/{key_id}", {
            params: { path: { key_id: keyId } },
          }),
        "protected",
      ),
    ),
};

export const usageApi = {
  summary: () =>
    unwrap<UsageSummary>(withAuthRetry(() => apiClient.GET("/user/usage/summary"), "protected")),
  volume: () =>
    unwrap<UsageVolume>(withAuthRetry(() => apiClient.GET("/user/usage/volume"), "protected")),
  latency: () =>
    unwrap<UsageLatency>(withAuthRetry(() => apiClient.GET("/user/usage/latency"), "protected")),
  records: (cursor?: string) =>
    unwrap<UsageRecords>(
      withAuthRetry(
        () =>
          apiClient.GET("/user/usage/records", {
            params: {
              query: {
                limit: productConfig.usage.recordsPageSize,
                ...(cursor ? { cursor } : {}),
              },
            },
          }),
        "protected",
      ),
    ),
  exportCsv: async () => {
    const data = await unwrap<string>(
      withAuthRetry(
        () => apiClient.GET("/user/usage/export.csv", { parseAs: "text" }),
        "protected",
      ),
    );
    return new Blob([data], { type: "text/csv;charset=utf-8" });
  },
};

export const historyApi = {
  list: (limit: number, offset: number, q?: string) =>
    unwrap<HistoryPage>(
      withAuthRetry(
        () =>
          apiClient.GET("/search/history", {
            params: { query: { limit, offset, ...(q ? { q } : {}) } },
          }),
        "protected",
      ),
    ),
  remove: (entryId: number) =>
    unwrap(
      withAuthRetry(
        () =>
          apiClient.DELETE("/search/history/{entry_id}", {
            params: { path: { entry_id: entryId } },
          }),
        "protected",
      ),
    ),
  removeMany: (ids: number[]) =>
    unwrap(
      withAuthRetry(
        () => apiClient.POST("/search/history/bulk-delete", { body: { ids } }),
        "protected",
      ),
    ),
};

export const surveyApi = {
  list: (view: SurveyView, cursor?: string) =>
    unwrap<SurveyList>(
      withAuthRetry(
        () =>
          apiClient.GET("/surveys", {
            params: { query: { view, limit: 20, ...(cursor ? { cursor } : {}) } },
          }),
        "protected",
      ),
    ),
  create: (body: SurveyCreateRequest) =>
    unwrap<Survey>(withAuthRetry(() => apiClient.POST("/surveys", { body }), "protected")),
  get: (surveyId: string) =>
    unwrap<Survey>(
      withAuthRetry(
        () => apiClient.GET("/surveys/{survey_id}", { params: { path: { survey_id: surveyId } } }),
        "protected",
      ),
    ),
  progress: (surveyId: string) =>
    unwrap<SurveyProgress>(
      withAuthRetry(
        () =>
          apiClient.GET("/surveys/{survey_id}/progress", {
            params: { path: { survey_id: surveyId } },
          }),
        "protected",
      ),
    ),
  drafts: (surveyId: string) =>
    unwrap<SurveyDraft[]>(
      withAuthRetry(
        () =>
          apiClient.GET("/surveys/{survey_id}/drafts", {
            params: { path: { survey_id: surveyId } },
          }),
        "protected",
      ),
    ),
  reviseDraft: (surveyId: string, body: SurveyDraftRequest) =>
    unwrap<SurveyDraft>(
      withAuthRetry(
        () =>
          apiClient.POST("/surveys/{survey_id}/drafts", {
            params: { path: { survey_id: surveyId } },
            body,
          }),
        "protected",
      ),
    ),
  saveManualDraft: (surveyId: string, body: SurveyManualDraftRequest) =>
    unwrap<SurveyDraft>(
      withAuthRetry(
        () =>
          apiClient.POST("/surveys/{survey_id}/drafts/manual", {
            params: { path: { survey_id: surveyId } },
            body,
          }),
        "protected",
      ),
    ),
  start: (surveyId: string, body: SurveyActionRequest) =>
    unwrap<Survey>(
      withAuthRetry(
        () =>
          apiClient.POST("/surveys/{survey_id}/start", {
            params: { path: { survey_id: surveyId } },
            body,
          }),
        "protected",
      ),
    ),
  cancel: (surveyId: string) =>
    unwrap<Survey>(
      withAuthRetry(
        () =>
          apiClient.POST("/surveys/{survey_id}/cancel", {
            params: { path: { survey_id: surveyId } },
          }),
        "protected",
      ),
    ),
  remove: (surveyId: string) =>
    unwrapEmpty(
      withAuthRetry(
        () =>
          apiClient.DELETE("/surveys/{survey_id}", {
            params: { path: { survey_id: surveyId } },
          }),
        "protected",
      ),
    ),
  report: (surveyId: string) =>
    unwrap<string>(
      withAuthRetry(
        () =>
          apiClient.GET("/surveys/{survey_id}/report", {
            params: { path: { survey_id: surveyId } },
            parseAs: "text",
          }),
        "protected",
      ),
    ),
  downloadPackage: (surveyId: string) =>
    unwrap<Blob>(
      withAuthRetry(
        () =>
          apiClient.GET("/surveys/{survey_id}/download", {
            params: { path: { survey_id: surveyId } },
            parseAs: "blob",
          }),
        "protected",
      ),
    ),
  artifacts: (surveyId: string) =>
    unwrap<SurveyArtifacts>(
      withAuthRetry(
        () =>
          apiClient.GET("/surveys/{survey_id}/artifacts", {
            params: { path: { survey_id: surveyId } },
          }),
        "protected",
      ),
    ),
};

export const adminApi = {
  capabilities: () =>
    unwrap<AdminCapabilities>(
      withAuthRetry(() => apiClient.GET("/admin/capabilities"), "protected"),
    ),
  analyticsOverview: (days = 30) =>
    unwrap<AdminAnalytics>(
      withAuthRetry(
        () => apiClient.GET("/admin/analytics/overview", { params: { query: { days } } }),
        "protected",
      ),
    ),
  operationsOverview: (days = 7, issueLimit = 20) =>
    unwrap<AdminOperations>(
      withAuthRetry(
        () =>
          apiClient.GET("/admin/operations/overview", {
            params: { query: { days, issue_limit: issueLimit } },
          }),
        "protected",
      ),
    ),
  lookupUser: (email: string) =>
    unwrap<AdminUserLookup>(
      withAuthRetry(
        () => apiClient.GET("/admin/users/lookup", { params: { query: { email } } }),
        "protected",
      ),
    ),
  updateQuotaOverrides: (userId: number, body: QuotaOverrideRequest) =>
    unwrap<QuotaOverrideUpdate>(
      withAuthRetry(
        () =>
          apiClient.PUT("/admin/users/{user_id}/quota-overrides", {
            params: { path: { user_id: userId } },
            body,
          }),
        "protected",
      ),
    ),
  auditEvents: (limit = 20) =>
    unwrap<AdminAuditEvent[]>(
      withAuthRetry(
        () => apiClient.GET("/admin/audit-events", { params: { query: { limit } } }),
        "protected",
      ),
    ),
};
