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
    establishSession(
      { access_token: "access", refresh_token: "refresh", token_type: "bearer" },
      false,
    );
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
});
