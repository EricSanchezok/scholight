import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LatencyChart, VolumeChart } from "./UsageCharts";

describe("usage charts", () => {
  it("renders a clear empty state instead of a zero-value chart", () => {
    render(<VolumeChart points={[]} />);
    expect(screen.getByText("No search activity in this period.")).toBeVisible();
  });

  it("breaks latency lines across null samples and retains the accessible data table", () => {
    const { container } = render(
      <LatencyChart
        points={[
          {
            bucket_start: "2026-07-20T00:00:00Z",
            standard_p50_ms: 500,
            thorough_p50_ms: 1000,
            overall_p95_ms: 1800,
            sample_count: 4,
          },
          {
            bucket_start: "2026-07-21T00:00:00Z",
            standard_p50_ms: null,
            thorough_p50_ms: 1100,
            overall_p95_ms: 1900,
            sample_count: 3,
          },
          {
            bucket_start: "2026-07-22T00:00:00Z",
            standard_p50_ms: 700,
            thorough_p50_ms: 1200,
            overall_p95_ms: 2100,
            sample_count: 5,
          },
        ]}
      />,
    );

    expect(container.querySelectorAll("path")).toHaveLength(2);
    expect(container.querySelectorAll("[data-latency-point]")).toHaveLength(8);
    expect(screen.getByRole("table", { name: "Daily response-time data" })).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: /Daily response time.*95th percentile/ }),
    ).toBeInTheDocument();
  });

  it("renders isolated latency samples as visible points without empty line paths", () => {
    const { container } = render(
      <LatencyChart
        points={[
          {
            bucket_start: "2026-07-23T00:00:00Z",
            standard_p50_ms: 11441.1,
            thorough_p50_ms: 3320.3,
            overall_p95_ms: 19202.4,
            sample_count: 4,
          },
        ]}
      />,
    );

    expect(container.querySelectorAll("path")).toHaveLength(0);
    expect(container.querySelectorAll("[data-latency-point]")).toHaveLength(3);
  });
});
