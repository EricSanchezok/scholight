import type { AccessTokenResponse } from "../api/types";
import { apiPath } from "../config/runtime";

const REFRESH_LOCK = "scholight-auth-refresh";
const CHANNEL_NAME = "scholight-auth";

let accessToken: string | null = null;
let refreshFlight: Promise<string> | null = null;
const channel = typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(CHANNEL_NAME) : null;
const localListeners = new Set<() => void>();

export function getAccessToken(): string | null {
  return accessToken;
}

export function establishSession(tokens: AccessTokenResponse, announce = true): void {
  accessToken = tokens.access_token;
  if (announce) channel?.postMessage({ type: "session-changed" });
}

function notifyLocalListeners(): void {
  localListeners.forEach((listener) => listener());
}

export function clearSession(announce = true): void {
  accessToken = null;
  if (announce) {
    notifyLocalListeners();
    channel?.postMessage({ type: "signed-out" });
  }
}

async function rotateRefreshToken(): Promise<string> {
  const response = await fetch(apiPath("/auth/refresh"), {
    method: "POST",
    credentials: "same-origin",
  });
  if (!response.ok) {
    if ([400, 401, 404].includes(response.status)) clearSession(false);
    throw new Error("Unable to refresh session");
  }
  const tokens = (await response.json()) as AccessTokenResponse;
  establishSession(tokens, false);
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
  localListeners.add(listener);
  const channelListener = () => listener();
  channel?.addEventListener("message", channelListener);
  return () => {
    localListeners.delete(listener);
    channel?.removeEventListener("message", channelListener);
  };
}
