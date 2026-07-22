import { describe, expect, it } from "vitest";

import { accessKeyStatus, expiryFromPreset, parseUserAgent, sortSessions } from "./account";

describe("account helpers", () => {
  it("turns a relative access-key expiry into UTC ISO time", () => {
    expect(expiryFromPreset("30", new Date("2026-07-22T10:00:00Z"))).toBe(
      "2026-08-21T10:00:00.000Z",
    );
  });

  it("distinguishes active, expired, and revoked access keys", () => {
    const now = new Date("2026-07-22T10:00:00Z");
    expect(accessKeyStatus({ expires_at: null, revoked_at: null }, now)).toBe("active");
    expect(accessKeyStatus({ expires_at: "2026-07-21T10:00:00Z", revoked_at: null }, now)).toBe(
      "expired",
    );
    expect(accessKeyStatus({ expires_at: null, revoked_at: "2026-07-20T10:00:00Z" }, now)).toBe(
      "revoked",
    );
  });

  it("parses common browser and operating-system user agents", () => {
    expect(
      parseUserAgent(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
      ),
    ).toBe("Chrome on macOS");
  });

  it("puts the current session first", () => {
    const sessions = [
      { id: 1, current: false, last_seen_at: "2026-07-22T09:00:00Z" },
      { id: 2, current: true, last_seen_at: "2026-07-21T09:00:00Z" },
    ];
    expect(sortSessions(sessions).map((session) => session.id)).toEqual([2, 1]);
  });
});
