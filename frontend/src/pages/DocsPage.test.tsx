import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { buildDeploymentUrls, DocsPage } from "./DocsPage";

describe("buildDeploymentUrls", () => {
  it("derives every public endpoint from the browser-visible deployment origin", () => {
    expect(buildDeploymentUrls("http://10.24.8.12:8080", "/api")).toEqual({
      web: "http://10.24.8.12:8080",
      api: "http://10.24.8.12:8080/api",
      search: "http://10.24.8.12:8080/api/search",
      mcp: "http://10.24.8.12:8080/api/mcp",
      openapi: "http://10.24.8.12:8080/api/openapi.json",
      interactiveApi: "http://10.24.8.12:8080/api/docs",
    });
  });

  it("normalizes trailing slashes without losing a configured API path", () => {
    expect(buildDeploymentUrls("https://papers.example.org/", "/gateway/api/").api).toBe(
      "https://papers.example.org/gateway/api",
    );
  });
});

describe("DocsPage", () => {
  it("renders integration guides with URLs for the current deployment", () => {
    const origin = "https://papers.internal.example";

    render(<DocsPage origin={origin} />);

    expect(screen.getByRole("heading", { name: "Build with Scholight" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Documentation" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Quick start" })).toHaveAttribute(
      "href",
      "#quick-start",
    );
    expect(screen.getAllByText(`${origin}/api`).length).toBeGreaterThan(0);
    expect(screen.getAllByText(new RegExp(`${origin}/api/search`)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(new RegExp(`${origin}/api/mcp`)).length).toBeGreaterThan(0);
    expect(
      screen.getAllByText(/SCHOLIGHT_API_URL=https:\/\/papers\.internal\.example\/api/).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText(/https:\/\/example\.com\/api/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open interactive API docs" })).toHaveAttribute(
      "href",
      `${origin}/api/docs`,
    );
  });

  it("confirms when a code example is copied", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);

    render(<DocsPage origin="http://127.0.0.1:5173" />);
    await user.click(screen.getAllByRole("button", { name: "Copy code" })[0]!);

    expect(writeText).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
  });
});
