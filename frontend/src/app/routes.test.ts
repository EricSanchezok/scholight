import { describe, expect, it } from "vitest";

import { accountRouteFor, accountRoutes, routes, visibleAccountRoutes, withQuery } from "./routes";

describe("route registry", () => {
  it("keeps every public path unique", () => {
    const paths = Object.values(routes)
      .map((route) => route.path)
      .filter((path) => path !== "*");
    expect(new Set(paths).size).toBe(paths.length);
  });

  it("keeps the approved account destination order", () => {
    expect(accountRoutes.map((route) => route.id)).toEqual([
      "usage",
      "accessKeys",
      "history",
      "account",
      "quotaAdmin",
    ]);
  });

  it("only exposes quota administration to capable users", () => {
    expect(visibleAccountRoutes(false).map((route) => route.id)).not.toContain("quotaAdmin");
    expect(visibleAccountRoutes(true).map((route) => route.id)).toContain("quotaAdmin");
  });

  it("resolves an account route from a pathname", () => {
    expect(accountRouteFor(routes.history.path)?.id).toBe("history");
  });

  it("builds encoded internal query strings", () => {
    expect(withQuery(routes.login.path, { returnTo: "/history?q=agents" })).toBe(
      "/login?returnTo=%2Fhistory%3Fq%3Dagents",
    );
  });
});
