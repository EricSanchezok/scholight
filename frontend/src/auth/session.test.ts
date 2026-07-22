import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  REFRESH_TOKEN_KEY,
  getAccessToken,
  refreshAccessToken,
  subscribeToSessionChanges,
} from "./session";

describe("session refresh", () => {
  beforeEach(() => {
    localStorage.setItem(REFRESH_TOKEN_KEY, "refresh-one");
    vi.restoreAllMocks();
  });

  it("coalesces concurrent refreshes into a single request", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          access_token: "access-two",
          refresh_token: "refresh-two",
          token_type: "bearer",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const [first, second] = await Promise.all([refreshAccessToken(), refreshAccessToken()]);
    expect(first).toBe("access-two");
    expect(second).toBe("access-two");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(getAccessToken()).toBe("access-two");
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBe("refresh-two");
  });

  it("notifies the current tab when refresh invalidates the session", async () => {
    const listener = vi.fn();
    const unsubscribe = subscribeToSessionChanges(listener);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid session" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(refreshAccessToken()).rejects.toThrow("Unable to refresh session");

    expect(listener).toHaveBeenCalledTimes(1);
    expect(getAccessToken()).toBeNull();
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBeNull();
    unsubscribe();
  });
});
