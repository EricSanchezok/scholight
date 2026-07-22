import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const result = {
  query: "retrieval augmented generation",
  strength: "standard",
  degraded: false,
  result_count: 1,
  elapsed_ms: 842,
  hits: [
    {
      rank: 1,
      score: 12.75,
      arxiv_id: "2401.12345",
      title: "A Paper About Retrieval",
      authors: ["Ada Lovelace", "Alan Turing"],
      abstract: "A focused study of retrieval for language models.",
      categories: ["cs.AI", "cs.IR"],
      submitted_at: "2024-01-20T00:00:00Z",
      updated_at: "2024-03-05T00:00:00Z",
      version: 2,
      arxiv_url: "https://arxiv.org/abs/2401.12345",
      pdf_url: "https://arxiv.org/pdf/2401.12345",
    },
  ],
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/search", (route) => route.fulfill({ json: result }));
  await page.route("**/api/auth/refresh", (route) =>
    route.fulfill({ status: 401, json: { detail: "Invalid session" } }),
  );
});

test("anonymous search reaches the continuous results view", async ({ page }) => {
  await page.goto("/");
  await page
    .getByRole("textbox", { name: "Search research papers" })
    .fill("retrieval augmented generation");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page).toHaveURL(/\/search\?q=retrieval\+augmented\+generation/);
  await expect(page.getByRole("heading", { name: "A Paper About Retrieval" })).toBeVisible();
  await expect(page.getByText("Score")).toBeVisible();
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(
    accessibility.violations.filter((item) => ["serious", "critical"].includes(item.impact ?? "")),
  ).toEqual([]);
});

test("protected routes preserve a safe return path", async ({ page }) => {
  await page.goto("/history?q=retrieval");
  await expect(page).toHaveURL(/\/login\?returnTo=%2Fhistory%3Fq%3Dretrieval/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("desktop home follows the Figma geometry", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop-only geometry assertion");
  await page.setViewportSize({ width: 1440, height: 1024 });
  await page.goto("/");

  const geometry = await page.evaluate(() => {
    const box = (selector: string) => {
      const element = document.querySelector(selector);
      if (!(element instanceof HTMLElement)) throw new Error(`Missing ${selector}`);
      const rect = element.getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
    };
    return {
      header: box("header"),
      title: box("main h1"),
      search: box('form[role="search"]'),
      strength: box('form[role="search"] select'),
      submit: box('form[role="search"] button[type="submit"]'),
    };
  });

  expect(geometry.header.height).toBe(88);
  expect(geometry.title).toEqual({ x: 160, y: 243, width: 920, height: 156 });
  expect(geometry.search).toEqual({ x: 160, y: 507, width: 1120, height: 72 });
  expect(geometry.strength.height).toBe(40);
  expect(geometry.submit).toMatchObject({ width: 112, height: 56 });
});

test("desktop results retain the left-aligned reading column", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop-only geometry assertion");
  await page.setViewportSize({ width: 1440, height: 1024 });
  await page.goto("/search?q=retrieval+augmented+generation&strength=standard");
  await expect(page.getByRole("heading", { name: "A Paper About Retrieval" })).toBeVisible();

  const geometry = await page.evaluate(() => {
    const rect = (selector: string) => {
      const element = document.querySelector(selector);
      if (!(element instanceof HTMLElement)) throw new Error(`Missing ${selector}`);
      const box = element.getBoundingClientRect();
      return { x: box.x, width: box.width };
    };
    return {
      search: rect('form[role="search"]'),
      summary: rect("main h1"),
      result: rect("article"),
    };
  });
  expect(geometry.search).toEqual({ x: 160, width: 1120 });
  expect(geometry.summary.x).toBe(160);
  expect(geometry.result).toEqual({ x: 160, width: 920 });
});

test("signed-in history follows the compact editorial layout", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop-only geometry assertion");
  await page.setViewportSize({ width: 1440, height: 1024 });
  await page.addInitScript(() => localStorage.setItem("scholight.refresh_token", "refresh-token"));
  await page.route("**/api/auth/refresh", (route) =>
    route.fulfill({
      json: { access_token: "access-token", refresh_token: "rotated-token", token_type: "bearer" },
    }),
  );
  await page.route("**/api/user/profile", (route) =>
    route.fulfill({
      json: {
        id: 1,
        email: "ada@example.com",
        display_name: "Ada Lovelace",
        email_verified: true,
        status: "active",
        created_at: "2026-07-01T08:00:00Z",
      },
    }),
  );
  await page.route("**/api/search/history**", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: 7,
            query: "reasoning in multimodal agents",
            strength: "thorough",
            filters: {},
            result_count: 10,
            elapsed_ms: 1840,
            created_at: "2026-07-19T14:35:00Z",
          },
        ],
        limit: 10,
        offset: 0,
        total: 1,
      },
    }),
  );
  await page.goto("/history");
  await expect(page.getByRole("heading", { name: "Search history" })).toBeVisible();

  const geometry = await page.evaluate(() => {
    const rect = (selector: string) => {
      const element = document.querySelector(selector);
      if (!(element instanceof HTMLElement)) throw new Error(`Missing ${selector}`);
      const box = element.getBoundingClientRect();
      return { width: box.width, height: box.height };
    };
    return {
      filter: rect('input[placeholder="Filter searches"]'),
      row: rect("article"),
    };
  });
  expect(geometry.filter.height).toBe(38);
  expect(geometry.row.height).toBe(98);
});

test("mobile home has no horizontal overflow", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "mobile-only assertion");
  await page.goto("/");
  const widths = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(widths.scroll).toBe(widths.client);
  await expect(page.getByRole("button", { name: "Menu" })).toBeVisible();
});
