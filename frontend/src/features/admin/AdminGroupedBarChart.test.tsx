import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { AdminGroupedBarChart } from "./AdminGroupedBarChart";

const sevenDays = [
  { day: "2026-07-18", primary: 2, secondary: 1 },
  { day: "2026-07-19", primary: 4, secondary: 2 },
  { day: "2026-07-20", primary: 0, secondary: 0 },
  { day: "2026-07-21", primary: 6, secondary: 3 },
  { day: "2026-07-22", primary: 1, secondary: 0 },
  { day: "2026-07-23", primary: 8, secondary: 5 },
  { day: "2026-07-24", primary: 12, secondary: 7 },
];

function renderChart(points = sevenDays) {
  return render(
    <AdminGroupedBarChart
      title="Daily Scholight search activity"
      description="Signed-in and anonymous searches."
      primaryLabel="Signed in"
      secondaryLabel="Anonymous"
      valueLabel="Searches"
      points={points}
    />,
  );
}

describe("AdminGroupedBarChart", () => {
  it("renders a labelled quantitative axis and exact sparse values", () => {
    renderChart();

    const chart = screen.getByRole("img", { name: /Daily Scholight search activity/ });
    expect(within(chart).getByText("Searches")).toBeInTheDocument();
    expect(within(chart).getByText("15")).toBeInTheDocument();
    expect(within(chart).getByText("10")).toBeInTheDocument();
    expect(chart.querySelector('[data-axis-tick="5"]')).toBeInTheDocument();
    expect(within(chart).getByText("12")).toBeInTheDocument();
    expect(within(chart).getByText("7")).toBeInTheDocument();
    expect(within(chart).getByText("Jul 18")).toBeInTheDocument();
    expect(within(chart).getByText("Jul 24")).toBeInTheDocument();
  });

  it("exposes exact values when a day is focused", async () => {
    const user = userEvent.setup();
    renderChart();

    const day = screen.getByRole("button", {
      name: "Jul 24: Signed in 12; Anonymous 7",
    });
    await user.click(day);
    day.focus();

    expect(screen.getByText("Jul 24 · Signed in 12 · Anonymous 7")).toBeInTheDocument();
  });

  it("uses five evenly distributed date labels for a 30-day chart", () => {
    const points = Array.from({ length: 30 }, (_, index) => ({
      day: `2026-06-${String(index + 1).padStart(2, "0")}`,
      primary: index + 1,
      secondary: index,
    }));
    renderChart(points);

    const chart = screen.getByRole("img", { name: /Daily Scholight search activity/ });
    expect(chart.querySelectorAll("[data-date-tick]")).toHaveLength(5);
  });
});
