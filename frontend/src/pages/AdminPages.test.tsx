import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { adminApi } from "../api/domain";
import type { AdminAnalytics, AdminOperations } from "../api/types";
import { AdminOperationsPage } from "./AdminOperationsPage";
import { AdminOverviewPage } from "./AdminOverviewPage";

vi.mock("../api/domain", () => ({
  adminApi: {
    analyticsOverview: vi.fn(),
    operationsOverview: vi.fn(),
  },
}));

const analytics: AdminAnalytics = {
  timezone: "UTC",
  from: "2026-06-25T00:00:00Z",
  to: "2026-07-25T00:00:00Z",
  profiles: {
    total: 1284,
    active: 1247,
    blocked: 37,
    admins: 3,
    created_in_period: 34,
  },
  searches: {
    total: 18420,
    authenticated: 12880,
    anonymous: 5540,
    standard: 13205,
    thorough: 5215,
    authenticated_rest: 11940,
    authenticated_mcp: 940,
    authenticated_success: 12700,
    authenticated_degraded: 150,
    authenticated_failed: 30,
    authenticated_p50_response_ms: 1200,
    authenticated_p95_response_ms: 4200,
  },
  access_keys: { total: 186, active: 179, used_in_period: 74 },
  daily: [
    {
      day: "2026-07-24",
      total: 120,
      authenticated: 80,
      anonymous: 40,
      standard: 90,
      thorough: 30,
      authenticated_rest: 70,
      authenticated_mcp: 10,
    },
  ],
};

const operations: AdminOperations = {
  timezone: "UTC",
  generated_at: "2026-07-24T08:00:00Z",
  sync: {
    last_successful_date: "2026-07-23",
    last_started_at: "2026-07-24T08:00:00Z",
    last_succeeded_at: "2026-07-24T08:12:00Z",
    last_error_code: null,
    last_error_message: null,
  },
  queue: {
    pending: 12,
    running: 1,
    retry: 2,
    succeeded: 300,
    dead: 1,
    oldest_waiting_at: "2026-07-24T07:00:00Z",
  },
  intake: [{ day: "2026-07-24", discovered: 20, full_text_completed: 17 }],
  recent_issues: [
    {
      arxiv_id: "2407.01512",
      target_version: 2,
      source: "revision",
      status: "retry",
      attempt_count: 2,
      max_attempts: 8,
      next_attempt_at: "2026-07-24T09:00:00Z",
      last_error_code: "source_unavailable",
      last_error_message: "Source temporarily unavailable",
      updated_at: "2026-07-24T08:30:00Z",
    },
  ],
};

function renderPage(page: React.ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{page}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("administration pages", () => {
  beforeEach(() => {
    vi.mocked(adminApi.analyticsOverview).mockReset().mockResolvedValue(analytics);
    vi.mocked(adminApi.operationsOverview).mockReset().mockResolvedValue(operations);
  });

  it("renders only backend-supported product overview metrics", async () => {
    renderPage(<AdminOverviewPage />);

    expect(await screen.findByText("1,284")).toBeInTheDocument();
    expect(screen.getByText("18,420")).toBeInTheDocument();
    expect(screen.getByText("12,880 signed in · 5,540 anonymous")).toBeInTheDocument();
    expect(screen.queryByText(/unmeasured/i)).not.toBeInTheDocument();
    expect(adminApi.analyticsOverview).toHaveBeenCalledWith(30);
  });

  it("refreshes the overview from the backend", async () => {
    const user = userEvent.setup();
    renderPage(<AdminOverviewPage />);

    await screen.findByText("Search activity");
    await user.click(screen.getByRole("button", { name: "Refresh administration overview" }));

    await waitFor(() => expect(adminApi.analyticsOverview).toHaveBeenCalledTimes(2));
  });

  it("renders and refreshes ingestion operations", async () => {
    const user = userEvent.setup();
    renderPage(<AdminOperationsPage />);

    expect(await screen.findByText("16 papers")).toBeInTheDocument();
    expect(screen.getByText("2407.01512")).toBeInTheDocument();
    expect(screen.getByText("Source temporarily unavailable")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Refresh operations" }));
    await waitFor(() => expect(adminApi.operationsOverview).toHaveBeenCalledTimes(2));
  });
});
