import { describe, expect, it } from "vitest";

import { accountRouteFor, accountRoutes, routes, withQuery } from "./routes";

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
    ]);
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
