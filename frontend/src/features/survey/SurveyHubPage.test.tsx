import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { surveyApi } from "../../api/domain";
import type { SurveyList, SurveySummary } from "../../api/types";
import { AuthContext, type AuthContextValue } from "../../auth/context";
import { I18nProvider } from "../../i18n/I18nProvider";
import { SurveyHubPage } from "./SurveyHubPage";

vi.mock("../../api/domain", () => ({
  surveyApi: {
    list: vi.fn(),
    create: vi.fn(),
    cancel: vi.fn(),
    remove: vi.fn(),
  },
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

const emptyList: SurveyList = {
  items: [],
  quota: { daily_limit: 3, reserved: 0, succeeded: 0, remaining: 3 },
  next_cursor: null,
};

const completedSurvey: SurveySummary = {
  id: "00000000-0000-0000-0000-000000000001",
  title: "Reasoning model evaluation",
  status: "succeeded",
  created_at: "2026-08-02T06:00:00Z",
  updated_at: "2026-08-02T07:00:00Z",
  started_at: "2026-08-02T06:10:00Z",
  finished_at: "2026-08-02T07:00:00Z",
  latest_draft_revision: 1,
  progress: {
    survey_id: "00000000-0000-0000-0000-000000000001",
    status: "succeeded",
    stage: "completed",
    percent: 100,
    step: 8,
    total_steps: 8,
    queue: null,
    elapsed_seconds: 3000,
    started_at: "2026-08-02T06:10:00Z",
    finished_at: "2026-08-02T07:00:00Z",
    last_activity_at: "2026-08-02T07:00:00Z",
  },
  report_available: true,
  artifacts_available: true,
};

function renderHub(auth: AuthContextValue = anonymous) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <AuthContext.Provider value={auth}>
          <MemoryRouter initialEntries={["/survey"]}>
            <SurveyHubPage />
          </MemoryRouter>
        </AuthContext.Provider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

describe("SurveyHubPage", () => {
  beforeEach(() => {
    vi.mocked(surveyApi.list).mockReset().mockResolvedValue(emptyList);
  });

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
    renderHub(authenticated);

    const start = screen.getByRole("button", { name: "Start survey" });
    expect(start).toBeEnabled();
    await user.click(start);
    expect(screen.getByText("Describe the research survey you want to run.")).toBeVisible();
  });

  it("defaults to completed history when no surveys are running", async () => {
    vi.mocked(surveyApi.list).mockImplementation((view) =>
      Promise.resolve(
        view === "completed" ? { ...emptyList, items: [completedSurvey] } : emptyList,
      ),
    );

    renderHub(authenticated);

    expect(
      await screen.findByRole("heading", { level: 3, name: "Reasoning model evaluation" }),
    ).toBeVisible();
  });

  it("respects an explicit switch back to the empty Running view", async () => {
    const user = userEvent.setup();
    vi.mocked(surveyApi.list).mockImplementation((view) =>
      Promise.resolve(
        view === "completed" ? { ...emptyList, items: [completedSurvey] } : emptyList,
      ),
    );
    renderHub(authenticated);
    await screen.findByRole("heading", { level: 3, name: "Reasoning model evaluation" });

    await user.click(screen.getByRole("tab", { name: "Running" }));

    expect(await screen.findByText("No surveys are running")).toBeVisible();
  });
});
