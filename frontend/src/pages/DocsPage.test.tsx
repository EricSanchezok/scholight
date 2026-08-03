import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { buildDeploymentUrls, DocsPage } from "./DocsPage";

describe("buildDeploymentUrls", () => {
  it("derives every public endpoint from the browser-visible deployment origin", () => {
    expect(buildDeploymentUrls("http://10.24.8.12:8080", "/api")).toEqual({
      search: "http://10.24.8.12:8080/api/search",
      extract: "http://10.24.8.12:8080/api/extract",
      mcp: "http://10.24.8.12:8080/api/mcp",
    });
  });

  it("normalizes trailing slashes without losing a configured API path", () => {
    expect(buildDeploymentUrls("https://papers.example.org/", "/gateway/api/").search).toBe(
      "https://papers.example.org/gateway/api/search",
    );
  });
});

describe("DocsPage", () => {
  it("renders integration guides with URLs for the current deployment", () => {
    const origin = "https://papers.internal.example";

    render(<DocsPage origin={origin} />);

    expect(screen.getByRole("heading", { name: "Using Scholight" })).toBeInTheDocument();
    expect(
      screen.getByText(
        "Search academic literature and retrieve readable source content from REST or an MCP-enabled agent.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/arXiv index/i)).not.toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Documentation" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Quick start" })).toHaveAttribute(
      "href",
      "#quick-start",
    );
    expect(screen.getAllByText(new RegExp(`${origin}/api/search`)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(new RegExp(`${origin}/api/mcp`)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(new RegExp(`${origin}/api/extract`)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/https:\/\/example\.com\/api/)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Open interactive API docs" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("View OpenAPI JSON")).not.toBeInTheDocument();
    expect(screen.queryByText("Inspect the Skill source")).not.toBeInTheDocument();
    expect(screen.queryByText("Deployment URLs")).not.toBeInTheDocument();
    expect(screen.queryByText("Live address.")).not.toBeInTheDocument();
    expect(screen.queryByText("Search Skill")).not.toBeInTheDocument();
    expect(screen.queryByText(/SCHOLIGHT_API_URL/)).not.toBeInTheDocument();
    expect(screen.queryByText(/github\.com\/EricSanchezok\/scholight/)).not.toBeInTheDocument();
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
