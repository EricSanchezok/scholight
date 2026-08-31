import { describe, expect, it } from "vitest";

import { detectInstallEnvironment } from "./install-environment";

describe("install environment", () => {
  it("recognizes Android browsers as installable", () => {
    expect(detectInstallEnvironment("Mozilla/5.0 (Linux; Android 14) Chrome/128")).toEqual({
      kind: "android",
      mobile: true,
      supported: true,
      standalone: false,
    });
  });

  it("offers iOS home-screen instructions", () => {
    expect(
      detectInstallEnvironment("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X Safari"),
    ).toEqual({
      kind: "ios",
      mobile: true,
      supported: true,
      standalone: false,
    });
  });

  it("routes in-app browsers to the manual guidance", () => {
    expect(detectInstallEnvironment("Mozilla/5.0 (iPhone) MicroMessenger/8.0")).toMatchObject({
      kind: "in-app",
      mobile: true,
      supported: true,
    });
  });

  it("does not add a mobile install entry on desktop", () => {
    expect(detectInstallEnvironment("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5)")).toMatchObject({
      mobile: false,
      supported: false,
    });
  });
});
