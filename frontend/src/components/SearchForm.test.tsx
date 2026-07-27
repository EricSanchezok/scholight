import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { dateFromPreset } from "../lib/format";
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

  it("replays existing filters and summarizes the active filter groups", async () => {
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
    expect(screen.getByRole("button", { name: "Filters · 3" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Search" }));

    expect(screen.getByTestId("location")).toHaveTextContent(
      "/search?q=agents&strength=standard&category=cs.AI&category=cs.LG&author=Ada+Lovelace&from=2024-01-01&to=2024-12-31",
    );
  });

  it("applies subject, date, author, and result-count controls to the search URL", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <SearchForm />
        <Location />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Filters" }));
    expect(screen.getByRole("dialog", { name: "Refine search" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Subject" }));
    await user.click(screen.getByRole("checkbox", { name: "Artificial Intelligence · cs.AI" }));
    await user.click(screen.getByRole("button", { name: "Publication date" }));
    await user.click(screen.getByRole("button", { name: "Past 6 months" }));
    await user.type(screen.getByRole("textbox", { name: "Author name" }), "Geoffrey Hinton");
    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("button", { name: "30 results" }));
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await user.type(
      screen.getByRole("textbox", { name: "Search research papers" }),
      "representation learning",
    );
    await user.click(screen.getByRole("button", { name: "Search" }));

    const expectedDate = dateFromPreset("6months");
    expect(screen.getByTestId("location")).toHaveTextContent(
      `/search?q=representation+learning&strength=standard&limit=30&category=cs.AI&author=Geoffrey+Hinton&from=${expectedDate}`,
    );
  });

  it("searches beyond the common AI subjects", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <SearchForm />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Filters" }));
    await user.click(screen.getByRole("button", { name: "Subject" }));
    await user.type(screen.getByRole("textbox", { name: "Find a subject" }), "algebraic geometry");

    expect(
      screen.getByRole("checkbox", { name: "Algebraic Geometry · math.AG" }),
    ).toBeInTheDocument();
  });

  it("reruns a compact results search when filters are applied", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/search?q=retrieval&strength=standard"]}>
        <SearchForm initialQuery="retrieval" compact />
        <Location />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Filters" }));
    await user.click(screen.getByRole("button", { name: "Subject" }));
    await user.click(screen.getByRole("checkbox", { name: "Machine Learning · cs.LG" }));
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    expect(screen.getByTestId("location")).toHaveTextContent(
      "/search?q=retrieval&strength=standard&category=cs.LG",
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
