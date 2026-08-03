import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { surveyApi } from "../../api/domain";
import { ApiError } from "../../api/errors";
import type { Survey, SurveyDraft, SurveyProgress } from "../../api/types";
import { I18nProvider } from "../../i18n/I18nProvider";
import { queryKeys } from "../../app/queryKeys";
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
  const view = render(
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
  return { client, ...view };
}

describe("SurveyDraftPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(surveyApi.get).mockResolvedValue(survey);
    vi.mocked(surveyApi.drafts).mockResolvedValue([draft]);
    vi.mocked(surveyApi.progress).mockResolvedValue(progress);
    vi.mocked(surveyApi.saveManualDraft).mockResolvedValue({ ...draft, revision: 2 });
    vi.mocked(surveyApi.start).mockResolvedValue({ ...survey, status: "queued" });
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

  it("keeps the loading surface mounted when draft work starts", async () => {
    const queuedDraft: SurveyDraft = {
      ...draft,
      markdown: null,
      status: "queued",
      started_at: null,
      finished_at: null,
    };
    vi.mocked(surveyApi.drafts).mockResolvedValue([queuedDraft]);
    const { client } = renderDraft();

    const loadingSurface = await screen.findByRole("status", {
      name: "Waiting to prepare research brief",
    });
    client.setQueryData(queryKeys.surveyDrafts(survey.id), [
      { ...queuedDraft, status: "running", started_at: "2026-07-31T10:01:00Z" },
    ]);

    const runningSurface = await screen.findByRole("status", {
      name: "Generating research brief",
    });
    expect(runningSurface).toBe(loadingSurface);
  });

  it("starts with completion email selected by default", async () => {
    const user = userEvent.setup();
    renderDraft();
    await screen.findByRole("heading", { name: "Draft history" });

    await user.click(screen.getByRole("button", { name: "Approve & start" }));
    const notification = screen.getByRole("checkbox", {
      name: "Email me when this survey finishes",
    });
    expect(notification).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Approve & start" }));

    expect(surveyApi.start).toHaveBeenCalledWith(
      survey.id,
      expect.objectContaining({ notify_on_completion: true }),
    );
  });

  it("preserves an unchecked choice when the confirmation dialog is reopened", async () => {
    const user = userEvent.setup();
    renderDraft();
    await screen.findByRole("heading", { name: "Draft history" });

    await user.click(screen.getByRole("button", { name: "Approve & start" }));
    const notification = screen.getByRole("checkbox", {
      name: "Email me when this survey finishes",
    });
    await user.click(notification);
    await user.click(screen.getByRole("button", { name: "Go back" }));
    await user.click(screen.getByRole("button", { name: "Approve & start" }));

    expect(
      screen.getByRole("checkbox", { name: "Email me when this survey finishes" }),
    ).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Approve & start" }));
    expect(surveyApi.start).toHaveBeenCalledWith(
      survey.id,
      expect.objectContaining({ notify_on_completion: false }),
    );
  });

  it("supports changing the email choice from the keyboard", async () => {
    const user = userEvent.setup();
    renderDraft();
    await screen.findByRole("heading", { name: "Draft history" });

    await user.click(screen.getByRole("button", { name: "Approve & start" }));
    const notification = screen.getByRole("checkbox", {
      name: "Email me when this survey finishes",
    });
    notification.focus();
    await user.keyboard("[Space]");

    expect(notification).not.toBeChecked();
  });

  it("reuses the notification choice and idempotency key after a retryable error", async () => {
    const user = userEvent.setup();
    vi.mocked(surveyApi.start)
      .mockRejectedValueOnce(new ApiError(503, "Try again", "temporary", true))
      .mockResolvedValueOnce({ ...survey, status: "queued" });
    renderDraft();
    await screen.findByRole("heading", { name: "Draft history" });

    await user.click(screen.getByRole("button", { name: "Approve & start" }));
    await user.click(screen.getByRole("checkbox", { name: "Email me when this survey finishes" }));
    await user.click(screen.getByRole("button", { name: "Approve & start" }));
    await screen.findByText("Try again");
    await user.click(screen.getByRole("button", { name: "Approve & start" }));

    await waitFor(() => expect(surveyApi.start).toHaveBeenCalledTimes(2));
    expect(vi.mocked(surveyApi.start).mock.calls[1]).toEqual(
      vi.mocked(surveyApi.start).mock.calls[0],
    );
    expect(vi.mocked(surveyApi.start).mock.calls[1]?.[1].notify_on_completion).toBe(false);
  });

  it("locks the email choice while the start request is pending", async () => {
    const user = userEvent.setup();
    vi.mocked(surveyApi.start).mockReturnValue(new Promise(() => undefined));
    renderDraft();
    await screen.findByRole("heading", { name: "Draft history" });

    await user.click(screen.getByRole("button", { name: "Approve & start" }));
    await user.click(screen.getByRole("button", { name: "Approve & start" }));

    expect(
      screen.getByRole("checkbox", { name: "Email me when this survey finishes" }),
    ).toBeDisabled();
  });
});
