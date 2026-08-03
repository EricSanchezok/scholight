import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { adminApi } from "../api/domain";
import type { AdminUserLookup } from "../api/types";
import { QuotaAdminPage } from "./QuotaAdminPage";

vi.mock("../api/domain", () => ({
  adminApi: {
    lookupUser: vi.fn(),
    updateQuotaOverrides: vi.fn(),
    auditEvents: vi.fn(),
  },
}));

const target: AdminUserLookup = {
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
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <QuotaAdminPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("quota administration page", () => {
  beforeEach(() => {
    vi.mocked(adminApi.lookupUser).mockReset().mockResolvedValue(target);
    vi.mocked(adminApi.updateQuotaOverrides).mockReset().mockResolvedValue({ changed: true });
    vi.mocked(adminApi.auditEvents).mockReset().mockResolvedValue([]);
  });

  it("finds one exact email and presents all three quota activities", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.type(screen.getByLabelText("User email"), "reader@example.com");
    await user.click(screen.getByRole("button", { name: "Find user" }));

    expect(await screen.findByText("Reader")).toBeInTheDocument();
    expect(screen.getByLabelText("Standard custom daily limit")).toHaveValue(5000);
    expect(screen.getByLabelText("Thorough custom daily limit")).toHaveValue(null);
    expect(screen.getByLabelText("Survey custom daily limit")).toHaveValue(2);
    expect(adminApi.lookupUser).toHaveBeenCalledWith("reader@example.com");
  });

  it("confirms the before and after values before an atomic save", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.type(screen.getByLabelText("User email"), "reader@example.com");
    await user.click(screen.getByRole("button", { name: "Find user" }));
    await screen.findByText("Reader");

    const standard = screen.getByLabelText("Standard custom daily limit");
    await user.clear(standard);
    await user.type(standard, "120");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText(/Standard: 5,000 → 120/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm changes" }));

    await waitFor(() =>
      expect(adminApi.updateQuotaOverrides).toHaveBeenCalledWith(7, {
        standard: 120,
        thorough: null,
        survey: 2,
      }),
    );
    await waitFor(() => expect(adminApi.lookupUser).toHaveBeenCalledTimes(2));
    expect(adminApi.auditEvents).toHaveBeenCalledTimes(2);
  });

  it("restores all three activities to deployment defaults", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.type(screen.getByLabelText("User email"), "reader@example.com");
    await user.click(screen.getByRole("button", { name: "Find user" }));
    await screen.findByText("Reader");

    await user.click(screen.getByRole("button", { name: "Restore defaults" }));
    await user.click(await screen.findByRole("button", { name: "Restore defaults" }));

    await waitFor(() =>
      expect(adminApi.updateQuotaOverrides).toHaveBeenCalledWith(7, {
        standard: null,
        thorough: null,
        survey: null,
      }),
    );
  });
});
