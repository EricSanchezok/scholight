import { beforeEach, describe, expect, it, vi } from "vitest";

import { clearSession, establishSession, getAccessToken, refreshAccessToken } from "./session";

describe("session refresh", () => {
  beforeEach(() => {
    clearSession(false);
    vi.restoreAllMocks();
  });

  it("coalesces cookie refreshes into one credentialed request", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          access_token: "access-two",
          token_type: "bearer",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const [first, second] = await Promise.all([refreshAccessToken(), refreshAccessToken()]);

    expect(first).toBe("access-two");
    expect(second).toBe("access-two");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/auth/refresh",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
      }),
    );
    expect(fetchMock.mock.calls[0]?.[1]).not.toHaveProperty("body");
    expect(getAccessToken()).toBe("access-two");
    expect(Object.keys(localStorage)).toHaveLength(0);
  });

  it("clears the in-memory access token when the cookie session is invalid", async () => {
    establishSession({ access_token: "stale-access", token_type: "bearer" }, false);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid session" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(refreshAccessToken()).rejects.toThrow("Unable to refresh session");

    expect(getAccessToken()).toBeNull();
    expect(Object.keys(localStorage)).toHaveLength(0);
  });
});
