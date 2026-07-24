import { beforeEach, describe, expect, it, vi } from "vitest";

const response = {
  query: "retrieval",
  strength: "standard" as const,
  degraded: false,
  hits: [],
  result_count: 0,
  elapsed_ms: 12,
};

describe("typed API client", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.restoreAllMocks();
    localStorage.clear();
    const NativeRequest = globalThis.Request;
    vi.stubGlobal(
      "Request",
      class AbsoluteRequest extends NativeRequest {
        constructor(input: RequestInfo | URL, init?: RequestInit) {
          super(
            typeof input === "string" && input.startsWith("/")
              ? new URL(input, "http://localhost")
              : input,
            init,
          );
        }
      },
    );
  });

  it("uses the fixed /api base and omits Authorization for anonymous search", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(response), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { searchApi } = await import("./domain");
    await searchApi.search({ query: "retrieval", strength: "standard", limit: 10, filters: {} });
    const request = fetchMock.mock.calls[0]?.[0];
    expect(request).toBeInstanceOf(Request);
    expect((request as Request).url).toContain("/api/search");
    expect((request as Request).headers.has("Authorization")).toBe(false);
  });

  it("sends the in-memory access token for signed-in search", async () => {
    const { establishSession } = await import("../auth/session");
    establishSession({ access_token: "access", token_type: "bearer" }, false);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(response), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { searchApi } = await import("./domain");
    await searchApi.search({ query: "retrieval", strength: "standard", limit: 10, filters: {} });
    const request = fetchMock.mock.calls[0]?.[0] as Request;
    expect(request.headers.get("Authorization")).toBe("Bearer access");
  });

  it("uses authenticated typed endpoints for private usage data", async () => {
    const { establishSession } = await import("../auth/session");
    establishSession({ access_token: "private-access", token_type: "bearer" }, false);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          today: {
            standard: { used: 1, daily_limit: 10, remaining: 9 },
            thorough: { used: 0, daily_limit: 2, remaining: 2 },
          },
          reset_at: "2026-07-23T00:00:00Z",
          timezone: "UTC",
          searches_today: 1,
          searches_this_month: 2,
          typical_response_ms: 800,
          p95_response_ms: 1400,
          success_rate: 1,
          degraded_count: 0,
          failed_count: 0,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const { usageApi } = await import("./domain");
    await usageApi.summary();
    const request = fetchMock.mock.calls[0]?.[0] as Request;
    expect(request.url).toContain("/api/user/usage/summary");
    expect(request.headers.get("Authorization")).toBe("Bearer private-access");
  });

  it("converts the authenticated CSV export response into a download blob", async () => {
    const { establishSession } = await import("../auth/session");
    establishSession({ access_token: "private-access", token_type: "bearer" }, false);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("created_at,strength\n2026-07-22,standard", {
        status: 200,
        headers: { "Content-Type": "text/csv" },
      }),
    );

    const { usageApi } = await import("./domain");
    const blob = await usageApi.exportCsv();
    expect(blob.type).toBe("text/csv;charset=utf-8");
    expect(blob.size).toBeGreaterThan(0);
  });

  it("sends both quota override fields through the protected admin endpoint", async () => {
    const { establishSession } = await import("../auth/session");
    establishSession({ access_token: "admin-access", token_type: "bearer" }, false);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ changed: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const { adminApi } = await import("./domain");
    await adminApi.updateQuotaOverrides(7, { standard: 5000, thorough: null });
    const request = fetchMock.mock.calls[0]?.[0] as Request;
    expect(request.url).toContain("/api/admin/users/7/quota-overrides");
    expect(await request.clone().json()).toEqual({ standard: 5000, thorough: null });
    expect(request.headers.get("Authorization")).toBe("Bearer admin-access");
  });

  it("accepts a successful empty response when an access key is revoked", async () => {
    const { establishSession } = await import("../auth/session");
    establishSession({ access_token: "private-access", token_type: "bearer" }, false);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));

    const { accessKeyApi } = await import("./domain");

    await expect(accessKeyApi.revoke("00000000-0000-0000-0000-000000000001")).resolves.toBe(
      undefined,
    );
  });
});
