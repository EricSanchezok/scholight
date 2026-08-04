import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { capabilitiesApi } from "../api/domain";
import { AuthContext, type AuthContextValue } from "../auth/context";
import { I18nProvider } from "../i18n/I18nProvider";
import { SiteHeader } from "./SiteHeader";

vi.mock("../api/domain", () => ({
  capabilitiesApi: { get: vi.fn() },
}));

const anonymous: AuthContextValue = {
  status: "anonymous",
  user: null,
  adminCapabilities: {
    can_manage_quotas: false,
    can_view_analytics: false,
    can_view_operations: false,
  },
  login: vi.fn(),
  logout: vi.fn(),
  refreshProfile: vi.fn(),
};

function renderHeader() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <AuthContext.Provider value={anonymous}>
          <MemoryRouter>
            <SiteHeader />
          </MemoryRouter>
        </AuthContext.Provider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

describe("SiteHeader capabilities", () => {
  beforeEach(() => {
    vi.mocked(capabilitiesApi.get).mockReset();
  });

  it("does not expose Survey while the capability is off", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue({ survey: "off" });

    renderHeader();

    expect(await screen.findByRole("link", { name: "Home" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Survey" })).not.toBeInTheDocument();
  });

  it("shows Survey after the capability is published", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue({ survey: "all" });

    renderHeader();

    expect(await screen.findByRole("link", { name: "Survey" })).toBeVisible();
  });
});
