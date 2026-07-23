import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PageRefreshButton } from "./PageRefreshButton";

describe("PageRefreshButton", () => {
  it("names the data being refreshed", () => {
    render(<PageRefreshButton label="search history" refreshing={false} onRefresh={vi.fn()} />);

    expect(screen.getByRole("button", { name: "Refresh search history" })).toHaveTextContent(
      "Refresh",
    );
  });

  it("announces and disables the in-flight refresh", () => {
    render(<PageRefreshButton label="usage and quota" refreshing onRefresh={vi.fn()} />);

    const button = screen.getByRole("button", { name: "Refreshing usage and quota" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveTextContent("Refreshing…");
  });
});
