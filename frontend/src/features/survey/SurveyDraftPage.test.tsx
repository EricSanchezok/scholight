import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { surveyApi } from "../../api/domain";
import type { Survey, SurveyDraft, SurveyProgress } from "../../api/types";
import { I18nProvider } from "../../i18n/I18nProvider";
import { SurveyDraftPage } from "./SurveyDraftPage";

vi.mock("../../api/domain", () => ({
  surveyApi: {
    get: vi.fn(),
    drafts: vi.fn(),
    progress: vi.fn(),
    reviseDraft: vi.fn(),
    saveManualDraft: vi.fn(),
    start: vi.fn(),
  },
}));

const survey: Survey = {
  id: "00000000-0000-0000-0000-000000000001",
  title: "AI and scientific work",
  initial_request: "AI and scientific work",
  status: "drafting",
  quota_state: "reserved",
  error_code: null,
  error_message: null,
  created_at: "2026-07-31T10:00:00Z",
  updated_at: "2026-07-31T10:05:00Z",
  started_at: null,
  finished_at: null,
};
const draft: SurveyDraft = {
  id: "00000000-0000-0000-0000-000000000002",
  revision: 1,
  source: "generated",
  user_message: "Focus on research roles",
  markdown: "# Research question\n\nHow will AI affect scientific work?",
  status: "ready",
  based_on_revision: null,
  error_code: null,
  error_message: null,
  created_at: "2026-07-31T10:00:00Z",
  started_at: "2026-07-31T10:01:00Z",
  finished_at: "2026-07-31T10:05:00Z",
};
const progress: SurveyProgress = {
  survey_id: survey.id,
  status: "drafting",
  stage: "drafting",
  percent: 0,
  step: 0,
  total_steps: 7,
  queue: null,
  elapsed_seconds: 0,
  started_at: null,
  finished_at: null,
  last_activity_at: "2026-07-31T10:05:00Z",
};

function renderDraft() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <MemoryRouter initialEntries={[`/survey/${survey.id}/draft`]}>
          <Routes>
            <Route path="/survey/:surveyId/draft" element={<SurveyDraftPage />} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

describe("SurveyDraftPage", () => {
  beforeEach(() => {
    vi.mocked(surveyApi.get).mockResolvedValue(survey);
    vi.mocked(surveyApi.drafts).mockResolvedValue([draft]);
    vi.mocked(surveyApi.progress).mockResolvedValue(progress);
    vi.mocked(surveyApi.saveManualDraft).mockResolvedValue({ ...draft, revision: 2 });
  });

  it("keeps draft history visible and discards local edits on Cancel", async () => {
    const user = userEvent.setup();
    renderDraft();
    expect(await screen.findByRole("heading", { name: "Draft history" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Edit draft" }));
    const editor = screen.getByRole("textbox", { name: "Markdown source" });
    await user.clear(editor);
    await user.type(editor, "# Changed");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Research question" })).toBeVisible(),
    );
    expect(surveyApi.saveManualDraft).not.toHaveBeenCalled();
  });

  it("saves a manual edit as a new revision", async () => {
    const user = userEvent.setup();
    renderDraft();
    await screen.findByRole("heading", { name: "Draft history" });
    await user.click(screen.getByRole("button", { name: "Edit draft" }));
    const editor = screen.getByRole("textbox", { name: "Markdown source" });
    await user.clear(editor);
    await user.type(editor, "# Updated brief");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(surveyApi.saveManualDraft).toHaveBeenCalledWith(
      survey.id,
      expect.objectContaining({ markdown: "# Updated brief", message: "Manual draft revision" }),
    );
  });
});
