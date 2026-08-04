import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { capabilitiesApi } from "../../api/domain";
import { SurveyAvailabilityRoute } from "./SurveyAvailabilityRoute";

vi.mock("../../api/domain", () => ({
  capabilitiesApi: { get: vi.fn() },
}));

function renderRoute() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/survey"]}>
        <Routes>
          <Route path="/" element={<h1>Home</h1>} />
          <Route element={<SurveyAvailabilityRoute />}>
            <Route path="/survey" element={<h1>Survey</h1>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SurveyAvailabilityRoute", () => {
  beforeEach(() => {
    vi.mocked(capabilitiesApi.get).mockReset();
  });

  it("renders Survey only when the public capability is all", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue({ survey: "all" });

    renderRoute();

    expect(await screen.findByRole("heading", { name: "Survey" })).toBeVisible();
  });

  it("redirects fail-closed when Survey is off", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue({ survey: "off" });

    renderRoute();

    expect(await screen.findByRole("heading", { name: "Home" })).toBeVisible();
  });

  it("redirects fail-closed when capability discovery fails", async () => {
    vi.mocked(capabilitiesApi.get).mockRejectedValue(new Error("unavailable"));

    renderRoute();

    await waitFor(() => expect(screen.getByRole("heading", { name: "Home" })).toBeVisible());
  });
});
