import { describe, expect, it } from "vitest";

import { toApiError } from "./errors";

describe("toApiError", () => {
  it("preserves a structured server error and retry contract", async () => {
    const error = await toApiError(
      new Response(null, { status: 503, headers: { "Retry-After": "5" } }),
      {
        detail: {
          code: "usage_service_unavailable",
          message: "Usage is temporarily unavailable.",
          retryable: true,
        },
      },
    );

    expect(error).toMatchObject({
      status: 503,
      code: "usage_service_unavailable",
      message: "Usage is temporarily unavailable.",
      retryable: true,
      retryAfter: 5,
    });
  });

  it("uses a product-neutral fallback for an unstructured 503", async () => {
    const error = await toApiError(new Response("Service Unavailable", { status: 503 }));

    expect(error.message).toBe("Scholight is temporarily unavailable. Please try again shortly.");
  });

  it("continues to accept legacy string detail", async () => {
    const error = await toApiError(new Response(null, { status: 401 }), {
      detail: "Session expired",
    });

    expect(error.message).toBe("Session expired");
  });
});
