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
  await page.clock.setFixedTime(new Date("2026-07-23T00:00:00Z"));
  await page.route("**/api/search", (route) => route.fulfill({ json: result }));
  await page.route("**/api/capabilities", (route) => route.fulfill({ json: { survey: "off" } }));
  await page.route("**/api/auth/refresh", (route) =>
    route.fulfill({ status: 401, json: { detail: "Invalid session" } }),
  );
});

async function mockSurveyAvailable(page: Page) {
  await page.route("**/api/capabilities", (route) => route.fulfill({ json: { survey: "all" } }));
}

async function mockAuthenticated(page: Page, canManageQuotas = false) {
  await page.route("**/api/auth/refresh", (route) =>
    route.fulfill({
      json: { access_token: "access-token", token_type: "bearer" },
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
  await page.route("**/api/admin/capabilities", (route) =>
    route.fulfill({ json: { can_manage_quotas: canManageQuotas } }),
  );
}

async function settleMotion(page: Page) {
  await page.waitForTimeout(50);
  await page.waitForFunction(() =>
    document
      .getAnimations()
      .every((animation) => animation.playState === "finished" || animation.playState === "idle"),
  );
}

async function mockAccountCenter(page: Page) {
  await mockAuthenticated(page);
  await page.route("**/api/user/avatar", (route) =>
    route.fulfill({
      json: {
        url: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='96' height='96'%3E%3Crect width='96' height='96' fill='%231f45b8'/%3E%3Ccircle cx='48' cy='38' r='17' fill='%23fbfaf5'/%3E%3Cpath d='M19 88c3-21 14-31 29-31s26 10 29 31' fill='%23fbfaf5'/%3E%3C/svg%3E",
        version: "00000000-0000-4000-8000-000000000001",
        expires_at: "2026-07-23T01:00:00Z",
      },
    }),
  );
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
          survey: { used: 1, daily_limit: 3, remaining: 2 },
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
            scopes: ["all"],
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
              scopes: ["all"],
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
              scopes: ["all"],
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

async function mockQuotaAdministration(page: Page) {
  await mockAuthenticated(page, true);
  await page.route("**/api/admin/users/lookup**", (route) =>
    route.fulfill({
      json: {
        user: {
          id: 7,
          email: "reader@example.com",
          display_name: "Reader",
          account_status: "active",
        },
        quotas: {
          standard: {
            default_limit: 1000,
            override_limit: 5000,
            effective_limit: 5000,
            used: 20,
            remaining: 4980,
          },
          thorough: {
            default_limit: 1000,
            override_limit: null,
            effective_limit: 1000,
            used: 4,
            remaining: 996,
          },
          survey: {
            default_limit: 3,
            override_limit: 2,
            effective_limit: 2,
            used: 1,
            remaining: 1,
          },
        },
      },
    }),
  );
  await page.route("**/api/admin/audit-events**", (route) =>
    route.fulfill({
      json: [
        {
          event_id: "00000000-0000-0000-0000-000000000001",
          actor_type: "user",
          actor_identifier: "admin@example.com",
          target_user_id: 7,
          target_email: "reader@example.com",
          action: "quota_overrides_updated",
          before_state: { standard: 1000, thorough: null, survey: null },
          after_state: { standard: 5000, thorough: null, survey: 2 },
          created_at: "2026-07-22T18:42:00Z",
        },
      ],
    }),
  );
  await page.route("**/api/admin/users/*/quota-overrides", (route) =>
    route.fulfill({ json: { changed: true } }),
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
  await expect(page.getByText("Searching the literature…")).toBeHidden();
  await settleMotion(page);
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(
    accessibility.violations.filter((item) => ["serious", "critical"].includes(item.impact ?? "")),
  ).toEqual([]);
});

test("anonymous survey hub prompts for sign-in without hiding the public shell", async ({
  page,
}) => {
  await mockSurveyAvailable(page);
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/survey");

  await expect(page.getByRole("heading", { name: "Research surveys" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Running" })).toBeVisible();
  await expect(page.getByText("Sign in to view your surveys")).toBeVisible();

  await page.getByRole("textbox", { name: "Describe the survey you want to start" }).focus();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Sign in to start a survey" })).toBeVisible();
  await settleMotion(page);

  const pageWidth = await page.locator("html").evaluate((element) => ({
    client: element.clientWidth,
    scroll: element.scrollWidth,
  }));
  expect(pageWidth.scroll).toBe(pageWidth.client);
  expect(
    (await new AxeBuilder({ page }).analyze()).violations.filter((item) =>
      ["serious", "critical"].includes(item.impact ?? ""),
    ),
  ).toEqual([]);
});

test("signed-in survey controls follow the shared page geometry", async ({ page }) => {
  await mockSurveyAvailable(page);
  await mockAuthenticated(page);
  await page.route("**/api/surveys**", (route) =>
    route.fulfill({
      json: {
        items: [],
        quota: { daily_limit: 3, reserved: 0, succeeded: 0, remaining: 3 },
        next_cursor: null,
      },
    }),
  );
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/survey");

  const input = page.getByRole("textbox", { name: "Describe the survey you want to start" });
  const refresh = page.getByRole("button", { name: "Refresh surveys" });
  const form = input.locator("xpath=..");
  const [refreshBox, formBox] = await Promise.all([refresh.boundingBox(), form.boundingBox()]);

  expect(refreshBox).not.toBeNull();
  expect(formBox).not.toBeNull();
  expect(refreshBox!.y + refreshBox!.height).toBeLessThan(formBox!.y);
  expect(refreshBox!.x).toBeGreaterThan(formBox!.x + formBox!.width / 2);

  await input.focus();
  expect(await input.evaluate((element) => getComputedStyle(element).outlineStyle)).toBe("none");

  await input.fill(
    "Compare how chain-of-thought supervision, process reward models, and outcome supervision affect reasoning robustness across mathematical, scientific, and code-generation benchmarks, including their assumptions, known failure modes, and recent empirical evidence.",
  );
  await expect
    .poll(async () => (await form.boundingBox())?.height ?? 0)
    .toBeGreaterThan(formBox!.height);
});

test("completed survey cards render a stable live Markdown preview", async ({ page }) => {
  await mockSurveyAvailable(page);
  await mockAuthenticated(page);
  const completedItems = [
    {
      id: "00000000-0000-0000-0000-000000000001",
      title: "Chain-of-thought compression and evaluation",
      elapsedSeconds: 5220,
    },
    {
      id: "00000000-0000-0000-0000-000000000002",
      title:
        "A deliberately longer survey title that wraps across several lines without shifting the report action",
      elapsedSeconds: 4980,
    },
    {
      id: "00000000-0000-0000-0000-000000000003",
      title: "Short survey",
      elapsedSeconds: 3600,
    },
  ].map(({ id, title, elapsedSeconds }) => ({
    id,
    title,
    status: "succeeded",
    created_at: "2026-08-02T06:00:00Z",
    updated_at: "2026-08-02T07:37:00Z",
    started_at: "2026-08-02T06:10:00Z",
    finished_at: "2026-08-02T07:37:00Z",
    latest_draft_revision: 1,
    progress: {
      survey_id: id,
      status: "succeeded",
      stage: "completed",
      percent: 100,
      step: 8,
      total_steps: 8,
      queue: null,
      elapsed_seconds: elapsedSeconds,
      started_at: "2026-08-02T06:10:00Z",
      finished_at: "2026-08-02T07:37:00Z",
      last_activity_at: "2026-08-02T07:37:00Z",
    },
    report_available: true,
    artifacts_available: true,
  }));
  await page.route("**/api/surveys?*", (route) =>
    route.fulfill({
      json: {
        items: completedItems,
        quota: { daily_limit: 3, reserved: 0, succeeded: 1, remaining: 2 },
        next_cursor: null,
      },
    }),
  );
  await page.route("**/api/surveys/*/report", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 150));
    await route.fulfill({
      contentType: "text/markdown",
      body: [
        "# Chain-of-thought compression and evaluation",
        "",
        "## Abstract",
        "",
        "This survey maps where reasoning tokens are saved and what accuracy trade-offs remain.",
        "",
        "## Evidence",
        "",
        "- Inference-time compression reduces generated tokens without retraining.",
        "- Post-training methods trade training cost for shorter reasoning traces.",
      ].join("\n"),
    });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/survey?view=completed");

  const preview = page.locator(".surveyReportThumbnail").first();
  const loadingBox = await preview.boundingBox();
  await expect(
    page.getByText("This survey maps where reasoning tokens are saved").first(),
  ).toBeVisible();
  const renderedBox = await preview.boundingBox();
  const paperBox = await page.locator(".surveyReportPaper").first().boundingBox();

  expect(renderedBox).toEqual(loadingBox);
  expect(paperBox).toEqual(renderedBox);
  await expect(page.getByText("Chain-of-thought compression and evaluation")).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Delete" })).toHaveCount(0);

  if ((page.viewportSize()?.width ?? 0) >= 1000) {
    const reportActions = page.locator(".surveyReportCardBody > strong");
    await expect(reportActions).toHaveCount(3);
    const actionBoxes = await reportActions.evaluateAll((elements) =>
      elements.map((element) => element.getBoundingClientRect()),
    );
    expect(new Set(actionBoxes.map(({ top }) => Math.round(top))).size).toBe(1);
  }
});

test("a failed survey preserves its draft and original request for reuse", async ({ page }) => {
  await mockSurveyAvailable(page);
  await mockAuthenticated(page);
  const failedId = "05544f18-fa64-4663-b085-62a76d96b308";
  const replacementId = "00000000-0000-0000-0000-000000000010";
  const initialRequest = [
    "数字生命构建：从 Agent 到自主互联网实体",
    "",
    "调研长期记忆、身份持久化、工具自主性和经济行为，区分已有证据与推测，并比较开放网络中的主要架构路线。",
  ].join("\n");
  const failedSurvey = {
    id: failedId,
    title: "数字生命构建：从Agent到自主互联网实体",
    initial_request: initialRequest,
    status: "failed",
    quota_state: "released",
    error_code: "survey_report_missing",
    error_message: "Research finished, but the final report could not be assembled.",
    created_at: "2026-08-16T09:31:46Z",
    updated_at: "2026-08-16T11:19:44Z",
    started_at: "2026-08-16T09:40:00Z",
    finished_at: "2026-08-16T11:19:44Z",
  };
  const progress = {
    survey_id: failedId,
    status: "failed",
    stage: "failed",
    percent: 100,
    step: 8,
    total_steps: 8,
    queue: null,
    elapsed_seconds: 5984,
    started_at: "2026-08-16T09:40:00Z",
    finished_at: "2026-08-16T11:19:44Z",
    last_activity_at: "2026-08-16T11:19:44Z",
  };
  await page.route("**/api/surveys?*", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            ...failedSurvey,
            latest_draft_revision: 1,
            progress,
            report_available: false,
            artifacts_available: true,
          },
        ],
        quota: { daily_limit: 3, reserved: 0, succeeded: 0, remaining: 3 },
        next_cursor: null,
      },
    }),
  );
  await page.route(`**/api/surveys/${failedId}`, (route) => route.fulfill({ json: failedSurvey }));
  await page.route(`**/api/surveys/${failedId}/drafts`, (route) =>
    route.fulfill({
      json: [
        {
          id: "00000000-0000-0000-0000-000000000002",
          revision: 1,
          source: "generated",
          user_message: initialRequest,
          markdown:
            "# Research brief\n\n## Scope\n\nMap the path from agents to persistent autonomous internet entities.",
          status: "ready",
          based_on_revision: null,
          error_code: null,
          error_message: null,
          created_at: "2026-08-16T09:31:46Z",
          started_at: "2026-08-16T09:32:00Z",
          finished_at: "2026-08-16T09:38:00Z",
        },
      ],
    }),
  );
  await page.route(`**/api/surveys/${failedId}/progress`, (route) =>
    route.fulfill({ json: progress }),
  );
  let replacementBody: unknown;
  await page.route("**/api/surveys", async (route) => {
    replacementBody = route.request().postDataJSON();
    await route.fulfill({
      status: 201,
      json: {
        ...failedSurvey,
        id: replacementId,
        status: "drafting",
        quota_state: "reserved",
        error_code: null,
        error_message: null,
        created_at: "2026-08-17T02:00:00Z",
        updated_at: "2026-08-17T02:00:00Z",
        started_at: null,
        finished_at: null,
      },
    });
  });
  await page.route(`**/api/surveys/${replacementId}`, (route) =>
    route.fulfill({
      json: {
        ...failedSurvey,
        id: replacementId,
        status: "drafting",
        quota_state: "reserved",
        error_code: null,
        error_message: null,
        started_at: null,
        finished_at: null,
      },
    }),
  );
  await page.route(`**/api/surveys/${replacementId}/drafts`, (route) =>
    route.fulfill({ json: [] }),
  );
  await page.route(`**/api/surveys/${replacementId}/progress`, (route) =>
    route.fulfill({
      json: { ...progress, survey_id: replacementId, status: "drafting", stage: "drafting" },
    }),
  );

  await page.goto("/survey?view=completed");
  await page.getByRole("link", { name: "Review draft →" }).click();

  await expect(page.getByText(/Original request/)).toBeVisible();
  await expect(page.getByText(/Draft and original request preserved/)).toBeVisible();
  await expect(page.locator(".surveyOriginalRequest").getByText(/调研长期记忆/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve & start" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Edit draft" })).toHaveCount(0);
  await settleMotion(page);

  const pageWidth = await page.locator("html").evaluate((element) => ({
    client: element.clientWidth,
    scroll: element.scrollWidth,
  }));
  expect(pageWidth.scroll).toBe(pageWidth.client);
  expect(
    (await new AxeBuilder({ page }).analyze()).violations.filter((item) =>
      ["serious", "critical"].includes(item.impact ?? ""),
    ),
  ).toEqual([]);

  await page.getByRole("button", { name: "Use request again" }).click();
  await expect(page).toHaveURL(new RegExp(`/survey/${replacementId}/draft$`));
  expect(replacementBody).toMatchObject({ initial_request: initialRequest });
  expect(replacementBody).toHaveProperty("client_request_id");
});

test("a completed report downloads cleanly and reflows for mobile reading", async ({
  page,
}, testInfo) => {
  await mockSurveyAvailable(page);
  await mockAuthenticated(page);
  const surveyId = "00000000-0000-0000-0000-000000000001";
  await page.route(`**/api/surveys/${surveyId}`, (route) =>
    route.fulfill({
      json: {
        id: surveyId,
        title: "模型架构验证闭环与推理效率评估体系的结构化研究报告",
        initial_request: "Survey reasoning compression.",
        status: "succeeded",
        quota_state: "consumed",
        error_code: null,
        error_message: null,
        created_at: "2026-08-02T06:00:00Z",
        updated_at: "2026-08-02T07:37:00Z",
        started_at: "2026-08-02T06:10:00Z",
        finished_at: "2026-08-02T07:37:00Z",
      },
    }),
  );
  await page.route(`**/api/surveys/${surveyId}/report`, (route) =>
    route.fulfill({
      contentType: "text/markdown",
      body: "# Report\n\nFinal paragraph.\n\n<!--M4-->",
    }),
  );
  await page.route(`**/api/surveys/${surveyId}/artifacts`, (route) =>
    route.fulfill({
      json: {
        survey_id: surveyId,
        expires_at: "2026-08-02T07:42:00Z",
        items: [],
      },
    }),
  );
  await page.route(`**/api/surveys/${surveyId}/download`, (route) =>
    route.fulfill({
      contentType: "application/zip",
      headers: {
        "Content-Disposition": `attachment; filename="scholight-survey-${surveyId}.zip"`,
      },
      body: "package",
    }),
  );
  await page.goto(`/survey/${surveyId}/report`);

  await expect(page.getByText("<!--M4-->")).not.toBeVisible();
  if (testInfo.project.name === "mobile") {
    const [titleBox, documentBox, detailsBox] = await Promise.all([
      page.locator(".surveyReportHeader h1").boundingBox(),
      page.locator(".surveyReportDocument").boundingBox(),
      page.locator(".surveyReportDetails").boundingBox(),
    ]);
    expect(titleBox).not.toBeNull();
    expect(documentBox).not.toBeNull();
    expect(detailsBox).not.toBeNull();
    expect(titleBox!.width).toBeGreaterThan(300);
    expect(documentBox!.width).toBeGreaterThan(300);
    expect(detailsBox!.y + detailsBox!.height).toBeLessThan(documentBox!.y);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    ).toBe(true);
  }
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download ZIP" }).click();
  const download = await downloadPromise;

  expect(download.suggestedFilename()).toBe(
    "模型架构验证闭环与推理效率评估体系的结构化研究报告.zip",
  );
});

test("survey routes remain unavailable while the public capability is off", async ({ page }) => {
  await page.goto("/survey");

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "Research surveys" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Survey" })).toHaveCount(0);
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
  await page.route("**/api/auth/refresh", (route) =>
    route.fulfill({
      json: { access_token: "access-token", token_type: "bearer" },
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
    "Usage & quotaAccess KeysSearch historyAccountSign out",
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

test("quota administration stays exact, auditable, and within the viewport", async ({ page }) => {
  await mockQuotaAdministration(page);
  await page.goto("/admin/quotas");
  await expect(page.getByRole("heading", { name: "Quota administration" })).toBeVisible();
  await page.getByLabel("User email").fill("reader@example.com");
  await page.getByRole("button", { name: "Find user" }).click();
  await expect(page.getByRole("heading", { name: "Reader" })).toBeVisible();
  await expect(page.getByLabel("Standard custom daily limit")).toHaveValue("5000");
  await expect(page.getByLabel("Thorough custom daily limit")).toHaveValue("");
  await expect(page.getByLabel("Survey custom daily limit")).toHaveValue("2");
  await expect(page.getByText(/Standard 1,000 → 5,000/)).toBeVisible();

  const widths = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(widths.scroll).toBe(widths.client);
  expect(
    (await new AxeBuilder({ page }).analyze()).violations.filter((item) =>
      ["serious", "critical"].includes(item.impact ?? ""),
    ),
  ).toEqual([]);
});

test("custom strength dropdown preserves the Figma interaction", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop-only visual assertion");
  await page.setViewportSize({ width: 1440, height: 1024 });
  await page.goto("/");
  await page.getByRole("combobox", { name: "Search strength" }).click();
  const standard = page.getByRole("option", { name: "Standard" });
  const thorough = page.getByRole("option", { name: "Thorough" });
  await expect(standard).toBeVisible();
  await expect(thorough).toBeVisible();
  await thorough.hover();
  await expect(thorough).toHaveCSS("color", "rgb(14, 15, 20)");
  await expect(thorough).toHaveCSS("background-color", "rgb(244, 242, 236)");
  await expect(standard).toHaveCSS("outline-style", "none");
  await settleMotion(page);
  await expect(page).toHaveScreenshot("strength-menu.png");
  await thorough.click();
  await expect(page.getByRole("combobox", { name: "Search strength" })).toHaveText("Thorough");
});

test("account menu uses the approved order and protected destinations", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop-only visual assertion");
  await page.setViewportSize({ width: 1440, height: 1024 });
  await mockAccountCenter(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Open account menu" }).click();
  await expect(page.getByRole("menuitem")).toHaveText([
    /Usage & quota/,
    /Access Keys/,
    /Search history/,
    /Account/,
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
  const keyLedgerWidth = await page.locator(".keyTableWrap").evaluate((element) => ({
    client: element.clientWidth,
    scroll: element.scrollWidth,
  }));
  expect(keyLedgerWidth.scroll).toBe(keyLedgerWidth.client);
  await expect(page).toHaveScreenshot("access-keys.png", { fullPage: true });
  await page.getByRole("button", { name: "Create new key" }).click();
  await page.getByRole("combobox", { name: "Expiration" }).click();
  await expect(page.getByRole("option", { name: "90 days from today" })).toBeVisible();
  await page.keyboard.press("Escape");

  await page.goto("/account");
  await expect(page.getByRole("heading", { name: "Account" })).toBeVisible();
  await expect(page.getByText("Chrome on macOS")).toBeVisible();
  await expect(page.getByText("Safari on macOS")).toBeVisible();
  await expect(page.getByRole("main").getByText("Eric Lin", { exact: true })).toBeVisible();
  await expect(page.getByRole("main").getByText("eric@example.com", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Manage SanchezCloud account" })).toHaveAttribute(
    "href",
    "https://myaccount.sanchezcloud.net",
  );
  await expect(page.getByRole("button", { name: "Save changes" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Change password" })).toHaveCount(0);
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
