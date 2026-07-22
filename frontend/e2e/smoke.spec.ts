import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

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

async function mockAuthenticated(page: Page) {
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
        email: "eric@example.com",
        display_name: "Eric Lin",
        email_verified: true,
        status: "active",
        created_at: "2026-07-01T08:00:00Z",
      },
    }),
  );
}

async function settleMotion(page: Page) {
  await page.waitForTimeout(300);
}

async function mockAccountCenter(page: Page) {
  await mockAuthenticated(page);
  const days = Array.from({ length: 12 }, (_, index) => {
    const day = String(index + 11).padStart(2, "0");
    return `2026-07-${day}T00:00:00Z`;
  });
  await page.route("**/api/user/usage/summary", (route) =>
    route.fulfill({
      json: {
        today: {
          standard: { used: 18, daily_limit: 100, remaining: 82 },
          thorough: { used: 4, daily_limit: 30, remaining: 26 },
        },
        reset_at: "2026-07-23T00:00:00Z",
        timezone: "UTC",
        searches_today: 22,
        searches_this_month: 184,
        typical_response_ms: 840,
        p95_response_ms: 3120,
        success_rate: 0.992,
        degraded_count: 2,
        failed_count: 1,
      },
    }),
  );
  await page.route("**/api/user/usage/volume", (route) =>
    route.fulfill({
      json: {
        from: days[0],
        to: days.at(-1),
        bucket: "day",
        points: days.map((bucket_start, index) => ({
          bucket_start,
          standard: [7, 10, 8, 13, 11, 15, 9, 17, 14, 18, 12, 16][index],
          thorough: [2, 3, 2, 4, 3, 5, 2, 4, 4, 6, 3, 5][index],
        })),
      },
    }),
  );
  await page.route("**/api/user/usage/latency", (route) =>
    route.fulfill({
      json: {
        from: days[0],
        to: days.at(-1),
        bucket: "day",
        points: days.map((bucket_start, index) => ({
          bucket_start,
          standard_p50_ms: 420 + index * 55,
          thorough_p50_ms: 980 + index * 95,
          overall_p95_ms: 1850 + index * 105,
          sample_count: 10,
        })),
      },
    }),
  );
  await page.route("**/api/user/usage/records**", (route) =>
    route.fulfill({
      json: {
        next_cursor: null,
        items: [
          {
            id: 1,
            created_at: "2026-07-22T18:42:00Z",
            actor_type: "access_key",
            access_key: { id: "a", name: "literature-review", last4: "7K2P" },
            strength: "thorough",
            search_duration_ms: 1840,
            result_count: 10,
            outcome: "success",
            quota_units: 1,
            status_code: 200,
            error_code: null,
          },
          {
            id: 2,
            created_at: "2026-07-22T18:29:00Z",
            actor_type: "web",
            access_key: null,
            strength: "standard",
            search_duration_ms: 710,
            result_count: 10,
            outcome: "success",
            quota_units: 1,
            status_code: 200,
            error_code: null,
          },
        ],
      },
    }),
  );
  await page.route("**/api/user/access-keys", (route) =>
    route.request().method() === "POST"
      ? route.fulfill({
          json: {
            id: "created",
            key: "sk_live_4f8ae91c2b76d0f53e89a7K2P",
            name: "literature-review",
            prefix: "sk_live_",
            last4: "7K2P",
            scopes: ["search"],
            created_at: "2026-07-22T18:42:00Z",
            last_used_at: null,
            expires_at: "2026-10-20T18:42:00Z",
            revoked_at: null,
          },
        })
      : route.fulfill({
          json: [
            {
              id: "a",
              name: "literature-review",
              prefix: "sk_live_",
              last4: "7K2P",
              scopes: ["search"],
              created_at: "2026-07-19T10:00:00Z",
              last_used_at: "2026-07-22T18:42:00Z",
              expires_at: null,
              revoked_at: null,
            },
            {
              id: "b",
              name: "cursor-agent",
              prefix: "sk_live_",
              last4: "P9MX",
              scopes: ["search"],
              created_at: "2026-07-03T10:00:00Z",
              last_used_at: "2026-07-21T09:15:00Z",
              expires_at: null,
              revoked_at: null,
            },
          ],
        }),
  );
  await page.route("**/api/user/access-keys/**", (route) =>
    route.fulfill({ json: { message: "Access key updated." } }),
  );
  await page.route("**/api/auth/sessions", (route) =>
    route.fulfill({
      json: [
        {
          id: 1,
          current: true,
          user_agent:
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36",
          created_at: "2026-07-20T08:00:00Z",
          last_seen_at: "2026-07-22T18:42:00Z",
          expires_at: "2026-08-20T08:00:00Z",
          revoked_at: null,
        },
        {
          id: 2,
          current: false,
          user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Version/18 Safari/605.1",
          created_at: "2026-07-20T08:00:00Z",
          last_seen_at: "2026-07-20T08:00:00Z",
          expires_at: "2026-08-20T08:00:00Z",
          revoked_at: null,
        },
      ],
    }),
  );
}

test("anonymous search reaches the continuous results view", async ({ page }) => {
  await page.goto("/");
  await page
    .getByRole("textbox", { name: "Search research papers" })
    .fill("retrieval augmented generation");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page).toHaveURL(/\/search\?q=retrieval\+augmented\+generation/);
  await expect(page.getByRole("heading", { name: "A Paper About Retrieval" })).toBeVisible();
  await expect(page.getByText("Score")).toBeVisible();
  await settleMotion(page);
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(
    accessibility.violations.filter((item) => ["serious", "critical"].includes(item.impact ?? "")),
  ).toEqual([]);
});

test("a delayed search immediately shows a stable editorial skeleton", async ({ page }) => {
  await page.route("**/api/search", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 500));
    await route.fulfill({ json: result });
  });
  await page.goto("/");
  await page.getByRole("textbox", { name: "Search research papers" }).fill("quiet interfaces");
  await page.getByRole("button", { name: "Search" }).click();

  await expect(page.getByTestId("search-results-skeleton")).toBeVisible();
  await expect(page.getByRole("button", { name: "Searching…" })).toHaveAttribute(
    "aria-busy",
    "true",
  );
  await expect(page.getByText("Searching the literature…")).toBeVisible();
  await expect(page.getByRole("heading", { name: "A Paper About Retrieval" })).toBeVisible();
  await expect(page.getByTestId("search-results-skeleton")).toBeHidden();
});

test("reduced motion keeps the search skeleton static", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.route("**/api/search", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 500));
    await route.fulfill({ json: result });
  });
  await page.goto("/search?q=quiet+interfaces&strength=standard");
  const pulse = page.getByRole("status", { name: "Loading search results" });
  await expect(pulse).toBeVisible();

  const firstOpacity = await pulse.evaluate((element) => getComputedStyle(element).opacity);
  await page.waitForTimeout(150);
  const secondOpacity = await pulse.evaluate((element) => getComputedStyle(element).opacity);
  expect(secondOpacity).toBe(firstOpacity);
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
      strength: box('form[role="search"] [role="combobox"]'),
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
  await expect(page.getByPlaceholder("Filter searches")).toBeVisible();

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

test("mobile account center keeps navigation and pages inside the viewport", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "mobile-only assertion");
  await mockAccountCenter(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Menu" }).click();
  await expect(page.getByRole("navigation", { name: "Mobile navigation" })).toContainText(
    "Usage & quotaAccess KeysSearch historyAccount settingsSign out",
  );

  for (const path of ["/usage", "/access-keys", "/account"]) {
    await page.goto(path);
    const widths = await page.evaluate(() => ({
      client: document.documentElement.clientWidth,
      scroll: document.documentElement.scrollWidth,
    }));
    expect(widths.scroll).toBe(widths.client);
  }
});

test("custom strength dropdown preserves the Figma interaction", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop-only visual assertion");
  await page.setViewportSize({ width: 1440, height: 1024 });
  await page.goto("/");
  await page.getByRole("combobox", { name: "Search strength" }).click();
  await expect(page.getByRole("option", { name: "Standard" })).toBeVisible();
  await expect(page.getByRole("option", { name: "Thorough" })).toBeVisible();
  await settleMotion(page);
  await expect(page).toHaveScreenshot("strength-menu.png");
  await page.getByRole("option", { name: "Thorough" }).click();
  await expect(page.getByRole("combobox", { name: "Search strength" })).toHaveText("Thorough");
});

test("account menu uses the approved order and protected destinations", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop-only visual assertion");
  await page.setViewportSize({ width: 1440, height: 1024 });
  await mockAuthenticated(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Open account menu" }).click();
  await expect(page.getByRole("menuitem")).toHaveText([
    /Usage & quota/,
    /Access Keys/,
    /Search history/,
    /Account settings/,
    /Sign out/,
  ]);
  await settleMotion(page);
  await expect(page).toHaveScreenshot("account-menu.png");
  await page.getByRole("menuitem", { name: /Usage & quota/ }).click();
  await expect(page).toHaveURL(/\/usage$/);
});

test("usage, access keys, and account match the editorial account center", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop-only account-center assertion");
  await page.setViewportSize({ width: 1440, height: 1024 });
  await mockAccountCenter(page);

  await page.goto("/usage");
  await expect(page.getByRole("heading", { name: "Usage & quota" })).toBeVisible();
  await expect(page.getByText("Access key · literature-review")).toBeVisible();
  await expect(page.getByRole("img", { name: "Daily search volume" })).toBeVisible();
  await expect(page.getByRole("img", { name: "Daily response time" })).toBeVisible();
  expect(
    (await new AxeBuilder({ page }).analyze()).violations.filter((item) =>
      ["serious", "critical"].includes(item.impact ?? ""),
    ),
  ).toEqual([]);
  await expect(page).toHaveScreenshot("usage.png", { fullPage: true });

  await page.goto("/access-keys");
  await expect(page.getByRole("heading", { name: "Access keys" })).toBeVisible();
  await expect(page.getByText("literature-review", { exact: true })).toBeVisible();
  await expect(page).toHaveScreenshot("access-keys.png", { fullPage: true });
  await page.getByRole("button", { name: "Create new key" }).click();
  await page.getByRole("combobox", { name: "Expiration" }).click();
  await expect(page.getByRole("option", { name: "90 days from today" })).toBeVisible();
  await page.keyboard.press("Escape");

  await page.goto("/account");
  await expect(page.getByRole("heading", { name: "Account settings" })).toBeVisible();
  await expect(page.getByText("Chrome on macOS")).toBeVisible();
  await expect(page.getByText("Safari on macOS")).toBeVisible();
  expect(
    (await new AxeBuilder({ page }).analyze()).violations.filter((item) =>
      ["serious", "critical"].includes(item.impact ?? ""),
    ),
  ).toEqual([]);
  await expect(page).toHaveScreenshot("account.png", { fullPage: true });
});

test("an access key secret is shown exactly once after creation", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop-only visual assertion");
  await page.setViewportSize({ width: 1440, height: 1024 });
  await mockAccountCenter(page);
  await page.goto("/access-keys");
  await page.getByRole("button", { name: "Create new key" }).click();
  await page.getByLabel("Key name").fill("literature-review");
  await page.getByRole("combobox", { name: "Expiration" }).click();
  await page.getByRole("option", { name: "90 days from today" }).click();
  await page.getByRole("button", { name: "Create key" }).click();

  await expect(page.getByRole("heading", { name: "Copy your key now" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "New access key" })).toHaveValue(
    "sk_live_4f8ae91c2b76d0f53e89a7K2P",
  );
  await expect(page).toHaveScreenshot("access-key-secret.png");
  await page.getByRole("button", { name: "Done" }).click();
  await expect(page.getByRole("heading", { name: "Copy your key now" })).toBeHidden();
});
