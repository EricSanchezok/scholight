import { toApiError } from "./errors";
import { apiClient, withAuthRetry, type ApiResult } from "./client";
import type {
  HistoryPage,
  LoginRequest,
  QuotaStatus,
  RegisterRequest,
  SearchRequest,
  SearchResponse,
  TokenResponse,
  UserProfile,
} from "./types";

async function unwrap<T>(promise: Promise<ApiResult<T>>): Promise<T> {
  const result = await promise;
  if (result.data !== undefined) return result.data;
  throw await toApiError(result.response, result.error);
}

export const authApi = {
  login: (body: LoginRequest) => unwrap<TokenResponse>(apiClient.POST("/auth/login", { body })),
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
  changePassword: (currentPassword: string, newPassword: string) =>
    unwrap(
      withAuthRetry(
        () =>
          apiClient.POST("/auth/change-password", {
            body: { current_password: currentPassword, new_password: newPassword },
          }),
        "protected",
      ),
    ),
  logout: () => unwrap(withAuthRetry(() => apiClient.POST("/auth/logout"), "protected")),
};

export const searchApi = {
  search: (body: SearchRequest) =>
    unwrap<SearchResponse>(withAuthRetry(() => apiClient.POST("/search", { body }))),
};

export const accountApi = {
  profile: () =>
    unwrap<UserProfile>(withAuthRetry(() => apiClient.GET("/user/profile"), "protected")),
  updateProfile: (displayName: string | null) =>
    unwrap<UserProfile>(
      withAuthRetry(
        () => apiClient.PATCH("/user/profile", { body: { display_name: displayName } }),
        "protected",
      ),
    ),
  quotas: () =>
    unwrap<QuotaStatus[]>(withAuthRetry(() => apiClient.GET("/user/quotas"), "protected")),
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
