import createClient from "openapi-fetch";

import { clearSession, getAccessToken, refreshAccessToken } from "../auth/session";
import { runtimeConfig } from "../config/runtime";
import type { paths } from "./schema";

export const apiClient = createClient<paths>({ baseUrl: runtimeConfig.apiBasePath });

apiClient.use({
  onRequest({ request }) {
    const token = getAccessToken();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    return request;
  },
});

export interface ApiResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

export async function withAuthRetry<T>(
  request: () => Promise<ApiResult<T>>,
  mode: "public" | "protected" = "public",
): Promise<ApiResult<T>> {
  let result = await request();
  if (result.response.status !== 401 || !getAccessToken()) return result;

  try {
    await refreshAccessToken();
    result = await request();
  } catch {
    clearSession();
    if (mode === "public") result = await request();
  }
  return result;
}
