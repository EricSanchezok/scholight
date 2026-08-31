import { describe, expect, it } from "vitest";

import { nextAvatarRefreshInterval } from "./avatar-refresh";

describe("nextAvatarRefreshInterval", () => {
  it("refreshes shortly before the earliest signed URL expires", () => {
    const now = Date.parse("2026-08-31T12:00:00Z");
    expect(nextAvatarRefreshInterval([{ expires_at: "2026-08-31T12:10:00Z" }], now)).toBe(
      9 * 60_000,
    );
  });

  it("keeps a minimum interval for already-expired URLs", () => {
    const now = Date.parse("2026-08-31T12:00:00Z");
    expect(nextAvatarRefreshInterval([{ expires_at: "2026-08-31T11:59:00Z" }], now)).toBe(30_000);
  });

  it("uses a calm polling interval when no avatar is available", () => {
    expect(nextAvatarRefreshInterval([null])).toBe(15 * 60_000);
  });
});
