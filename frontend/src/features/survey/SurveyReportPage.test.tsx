import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { surveyApi } from "../../api/domain";
import { ApiError } from "../../api/errors";
import type { Survey } from "../../api/types";
import { I18nProvider } from "../../i18n/I18nProvider";
import { SurveyReportPage } from "./SurveyReportPage";

vi.mock("../../api/domain", () => ({
  surveyApi: {
    get: vi.fn(),
    report: vi.fn(),
    artifacts: vi.fn(),
    remove: vi.fn(),
    downloadPackage: vi.fn(),
    downloadPdf: vi.fn(),
    create: vi.fn(),
  },
}));

const survey: Survey = {
  id: "00000000-0000-0000-0000-000000000001",
  title: "Reliable model evaluation",
  initial_request: "Compare model architectures and inference efficiency.",
  status: "succeeded",
  quota_state: "consumed",
  error_code: null,
  error_message: null,
  created_at: "2026-08-19T06:00:00Z",
  updated_at: "2026-08-19T07:00:00Z",
  started_at: "2026-08-19T06:05:00Z",
  finished_at: "2026-08-19T07:00:00Z",
};

function renderReport() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <MemoryRouter initialEntries={[`/survey/${survey.id}/report`]}>
          <Routes>
            <Route path="/survey/:surveyId/report" element={<SurveyReportPage />} />
            <Route path="/survey/:surveyId/draft" element={<p>Replacement draft</p>} />
            <Route path="/survey" element={<p>Survey history</p>} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

describe("SurveyReportPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(surveyApi.get).mockResolvedValue(survey);
    vi.mocked(surveyApi.report).mockResolvedValue(
      "# Reliable model evaluation\n\n## Abstract\n\nEvidence-backed report.",
    );
    vi.mocked(surveyApi.artifacts).mockResolvedValue({
      survey_id: survey.id,
      expires_at: "2026-09-19T07:00:00Z",
      items: [],
    });
    vi.mocked(surveyApi.remove).mockResolvedValue(undefined);
    vi.mocked(surveyApi.downloadPackage).mockResolvedValue(new Blob());
    vi.mocked(surveyApi.create).mockResolvedValue({
      ...survey,
      id: "00000000-0000-0000-0000-000000000002",
      status: "drafting",
      quota_state: "reserved",
    });
  });

  it("creates a new Survey from the original request without replacing the report", async () => {
    const user = userEvent.setup();
    renderReport();

    await user.click(await screen.findByRole("button", { name: "Run again" }));

    await waitFor(() =>
      expect(surveyApi.create).toHaveBeenCalledWith({
        initial_request: survey.initial_request,
        client_request_id: expect.any(String),
      }),
    );
    expect(await screen.findByText("Replacement draft")).toBeVisible();
  });

  it("offers owner-scoped deletion when report loading fails", async () => {
    const user = userEvent.setup();
    vi.mocked(surveyApi.report).mockRejectedValue(
      new ApiError(
        503,
        "Survey artifacts are temporarily unavailable.",
        "survey_archive_error",
        true,
      ),
    );
    renderReport();

    expect(
      await screen.findByRole("heading", { name: "Unable to open this report" }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(surveyApi.remove).toHaveBeenCalledWith(survey.id));
  });

  it("keeps a degraded report readable and explains that it was not charged", async () => {
    vi.mocked(surveyApi.get).mockResolvedValue({
      ...survey,
      quota_state: "released",
      error_code: "survey_quality_degraded",
      error_message:
        "This report was delivered with incomplete quality checks and was not counted against your Survey allowance.",
    });

    renderReport();

    expect(await screen.findByText("Report delivered with quality notes")).toBeVisible();
    expect(screen.getByText(/not counted against your Survey allowance/i)).toBeVisible();
    expect(screen.getAllByRole("heading", { name: "Reliable model evaluation" })).toHaveLength(2);
  });
});
