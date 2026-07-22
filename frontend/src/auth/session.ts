import type { TokenResponse } from "../api/types";

export const REFRESH_TOKEN_KEY = "scholight.refresh_token";
const REFRESH_LOCK = "scholight-auth-refresh";
const CHANNEL_NAME = "scholight-auth";

let accessToken: string | null = null;
let refreshFlight: Promise<string> | null = null;
const channel = typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(CHANNEL_NAME) : null;

export function getAccessToken(): string | null {
  return accessToken;
}

export function hasRefreshToken(): boolean {
  return Boolean(localStorage.getItem(REFRESH_TOKEN_KEY));
}

export function establishSession(tokens: TokenResponse, announce = true): void {
  accessToken = tokens.access_token;
  localStorage.setItem(REFRESH_TOKEN_KEY, tokens.refresh_token);
  if (announce) channel?.postMessage({ type: "session-changed" });
  window.dispatchEvent(new Event("scholight-session"));
}

export function clearSession(announce = true): void {
  accessToken = null;
  localStorage.removeItem(REFRESH_TOKEN_KEY);
  if (announce) channel?.postMessage({ type: "signed-out" });
  window.dispatchEvent(new Event("scholight-session"));
}

async function rotateRefreshToken(): Promise<string> {
  const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY);
  if (!refreshToken) throw new Error("No refresh token is available");

  const response = await fetch("/api/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
  if (!response.ok) {
    if ([400, 401, 404].includes(response.status)) clearSession();
    throw new Error("Unable to refresh session");
  }
  const tokens = (await response.json()) as TokenResponse;
  establishSession(tokens);
  return tokens.access_token;
}

export function refreshAccessToken(): Promise<string> {
  if (refreshFlight) return refreshFlight;

  const run = async () => {
    const locks = navigator.locks;
    if (locks) return locks.request(REFRESH_LOCK, rotateRefreshToken);
    return rotateRefreshToken();
  };

  refreshFlight = run().finally(() => {
    refreshFlight = null;
  });
  return refreshFlight;
}

export function subscribeToSessionChanges(listener: () => void): () => void {
  const storageListener = (event: StorageEvent) => {
    if (event.key === REFRESH_TOKEN_KEY) listener();
  };
  const channelListener = () => listener();
  window.addEventListener("storage", storageListener);
  window.addEventListener("scholight-session", listener);
  channel?.addEventListener("message", channelListener);
  return () => {
    window.removeEventListener("storage", storageListener);
    window.removeEventListener("scholight-session", listener);
    channel?.removeEventListener("message", channelListener);
  };
}
