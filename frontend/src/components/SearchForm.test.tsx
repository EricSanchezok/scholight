import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { SearchForm } from "./SearchForm";

function Location() {
  return <output data-testid="location">{useLocation().pathname + useLocation().search}</output>;
}

describe("SearchForm", () => {
  it("keeps strength controls compact and serializes the query", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <SearchForm />
        <Location />
      </MemoryRouter>,
    );
    await user.type(
      screen.getByRole("textbox", { name: "Search research papers" }),
      "graph neural networks",
    );
    await user.click(screen.getByRole("combobox", { name: "Search strength" }));
    await user.click(screen.getByRole("option", { name: "Thorough" }));
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/search?q=graph+neural+networks&strength=thorough",
    );
  });

  it("replays existing hidden filters without exposing an advanced filter control", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <SearchForm
          filters={{
            categories: ["cs.AI", "cs.LG"],
            authors: ["Ada Lovelace"],
            date_from: "2024-01-01",
            date_to: "2024-12-31",
          }}
        />
        <Location />
      </MemoryRouter>,
    );

    await user.type(screen.getByRole("textbox", { name: "Search research papers" }), "agents");
    expect(screen.queryByRole("button", { name: "Filters" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Search" }));

    expect(screen.getByTestId("location")).toHaveTextContent(
      "/search?q=agents&strength=standard&category=cs.AI&category=cs.LG&author=Ada+Lovelace&from=2024-01-01&to=2024-12-31",
    );
  });

  it("shows a query validation error without navigating", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <SearchForm />
        <Location />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a research question");
    expect(screen.getByTestId("location")).toHaveTextContent("/");
  });

  it("announces a pending search without moving or disabling the query field", () => {
    render(
      <MemoryRouter>
        <SearchForm initialQuery="quiet interfaces" busy />
      </MemoryRouter>,
    );

    expect(screen.getByRole("textbox", { name: "Search research papers" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Searching…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Searching…" })).toHaveAttribute("aria-busy", "true");
  });
});
