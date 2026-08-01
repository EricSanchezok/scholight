import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../../auth/context";
import { I18nProvider } from "../../i18n/I18nProvider";
import { SurveyHubPage } from "./SurveyHubPage";

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

function renderHub() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <AuthContext.Provider value={anonymous}>
          <MemoryRouter initialEntries={["/survey"]}>
            <SurveyHubPage />
          </MemoryRouter>
        </AuthContext.Provider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

describe("SurveyHubPage", () => {
  it("shows the public Survey shell and a signed-out list state", () => {
    renderHub();
    expect(screen.getByRole("heading", { name: "Research surveys" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Running" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Sign in to view your surveys" })).toBeVisible();
  });

  it("opens the sign-in dialog as soon as an anonymous user focuses the request", async () => {
    const user = userEvent.setup();
    renderHub();
    await user.click(screen.getByLabelText("Describe the survey you want to start"));
    expect(screen.getByRole("dialog")).toHaveTextContent("Sign in to start a survey");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("uses an active primary action and explains an empty signed-in request", async () => {
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const authenticated: AuthContextValue = {
      ...anonymous,
      status: "authenticated",
      user: {
        id: 1,
        email: "researcher@example.com",
        display_name: "Researcher",
        status: "active",
        email_verified: true,
      },
    };
    render(
      <QueryClientProvider client={client}>
        <I18nProvider>
          <AuthContext.Provider value={authenticated}>
            <MemoryRouter initialEntries={["/survey"]}>
              <SurveyHubPage />
            </MemoryRouter>
          </AuthContext.Provider>
        </I18nProvider>
      </QueryClientProvider>,
    );

    const start = screen.getByRole("button", { name: "Start survey" });
    expect(start).toBeEnabled();
    await user.click(start);
    expect(screen.getByText("Describe the research survey you want to run.")).toBeVisible();
  });
});
