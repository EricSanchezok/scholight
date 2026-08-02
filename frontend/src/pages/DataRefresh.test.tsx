import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { accessKeyApi, accountApi, authApi, historyApi, usageApi } from "../api/domain";
import { AccessKeysPage } from "./AccessKeysPage";
import { AccountPage } from "./AccountPage";
import { HistoryPage } from "./HistoryPage";
import { UsagePage } from "./UsagePage";

vi.mock("../api/domain", () => ({
  accessKeyApi: {
    list: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    revoke: vi.fn(),
  },
  accountApi: {
    profile: vi.fn(),
    updateProfile: vi.fn(),
    sessions: vi.fn(),
    revokeSession: vi.fn(),
    revokeOtherSessions: vi.fn(),
  },
  authApi: {
    changePassword: vi.fn(),
  },
  historyApi: {
    list: vi.fn(),
    remove: vi.fn(),
    removeMany: vi.fn(),
  },
  usageApi: {
    summary: vi.fn(),
    volume: vi.fn(),
    latency: vi.fn(),
    records: vi.fn(),
    exportCsv: vi.fn(),
  },
}));

vi.mock("../auth/context", () => ({
  useAuth: () => ({
    user: {
      id: 7,
      email: "reader@example.com",
      display_name: "Reader",
      status: "active",
      email_verified: true,
    },
    refreshProfile: vi.fn(),
  }),
}));

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

describe("private data refresh controls", () => {
  beforeEach(() => {
    vi.mocked(accessKeyApi.list).mockReset().mockResolvedValue([]);
    vi.mocked(accountApi.profile).mockReset().mockResolvedValue({
      id: 7,
      email: "reader@example.com",
      display_name: "Reader",
      status: "active",
      email_verified: true,
    });
    vi.mocked(accountApi.sessions).mockReset().mockResolvedValue([]);
    vi.mocked(authApi.changePassword).mockReset();
    vi.mocked(historyApi.list).mockReset().mockResolvedValue({
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
    });
    vi.mocked(usageApi.summary)
      .mockReset()
      .mockResolvedValue({
        today: {
          standard: { used: 0, daily_limit: 10, remaining: 10 },
          thorough: { used: 0, daily_limit: 2, remaining: 2 },
          survey: { used: 1, daily_limit: 3, remaining: 2 },
        },
        reset_at: "2026-07-24T00:00:00Z",
        timezone: "UTC",
        searches_today: 0,
        searches_this_month: 0,
        typical_response_ms: null,
        p95_response_ms: null,
        success_rate: null,
        degraded_count: 0,
        failed_count: 0,
      });
    vi.mocked(usageApi.volume).mockReset().mockResolvedValue({
      bucket: "day",
      from: "2026-06-24T00:00:00Z",
      to: "2026-07-23T23:59:59Z",
      points: [],
    });
    vi.mocked(usageApi.latency).mockReset().mockResolvedValue({
      bucket: "day",
      from: "2026-06-24T00:00:00Z",
      to: "2026-07-23T23:59:59Z",
      points: [],
    });
    vi.mocked(usageApi.records).mockReset().mockResolvedValue({
      items: [],
      next_cursor: null,
    });
  });

  it("refreshes the current history query", async () => {
    const user = userEvent.setup();
    renderPage(<HistoryPage />);
    const refresh = await screen.findByRole("button", { name: "Refresh search history" });

    await user.click(refresh);

    await waitFor(() => expect(historyApi.list).toHaveBeenCalledTimes(2));
  });

  it("refreshes the access-key ledger", async () => {
    const user = userEvent.setup();
    renderPage(<AccessKeysPage />);
    const refresh = await screen.findByRole("button", { name: "Refresh access keys" });

    await user.click(refresh);

    await waitFor(() => expect(accessKeyApi.list).toHaveBeenCalledTimes(2));
  });

  it("refreshes quota, analytics, and recent usage together", async () => {
    const user = userEvent.setup();
    renderPage(<UsagePage />);
    const refresh = await screen.findByRole("button", { name: "Refresh usage and quota" });

    await user.click(refresh);

    await waitFor(() => {
      expect(usageApi.summary).toHaveBeenCalledTimes(2);
      expect(usageApi.volume).toHaveBeenCalledTimes(2);
      expect(usageApi.latency).toHaveBeenCalledTimes(2);
      expect(usageApi.records).toHaveBeenCalledTimes(2);
    });
  });

  it("shows today's Survey allowance with the search quotas", async () => {
    renderPage(<UsagePage />);

    expect(await screen.findByRole("progressbar", { name: "survey quota used" })).toHaveAttribute(
      "aria-valuemax",
      "3",
    );
  });

  it("refreshes active sessions independently", async () => {
    const user = userEvent.setup();
    renderPage(<AccountPage />);
    const refresh = await screen.findByRole("button", { name: "Refresh active sessions" });

    await user.click(refresh);

    await waitFor(() => expect(accountApi.sessions).toHaveBeenCalledTimes(2));
  });
});
