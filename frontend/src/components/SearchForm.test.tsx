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
    await user.selectOptions(screen.getByRole("combobox", { name: "Search strength" }), "thorough");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/search?q=graph+neural+networks&strength=thorough",
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
});
