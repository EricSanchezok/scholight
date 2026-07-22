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

  it("serializes category author and date filters from the form", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <SearchForm />
        <Location />
      </MemoryRouter>,
    );

    await user.type(screen.getByRole("textbox", { name: "Search research papers" }), "agents");
    await user.click(screen.getByRole("button", { name: "Filters" }));
    await user.type(screen.getByRole("textbox", { name: "Categories" }), "cs.AI, cs.LG");
    await user.type(screen.getByRole("textbox", { name: "Authors" }), "Ada Lovelace");
    await user.type(screen.getByLabelText("From date"), "2024-01-01");
    await user.type(screen.getByLabelText("To date"), "2024-12-31");
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
});
