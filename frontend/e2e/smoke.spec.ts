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
  await expect(page.getByRole("heading", { name: "Sign in to Scholight" })).toBeVisible();
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
