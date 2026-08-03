import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SurveyMarkdown } from "./SurveyMarkdown";

describe("SurveyMarkdown", () => {
  it("renders GFM content without rendering raw HTML", () => {
    const { container } = render(
      <SurveyMarkdown
        markdown={
          "# Report\n\n| Finding | Result |\n| --- | --- |\n| A | B |\n\n<script>alert(1)</script>"
        }
      />,
    );
    expect(screen.getByRole("heading", { name: "Report" })).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(container.querySelector("script")).not.toBeInTheDocument();
  });

  it("renders manifest-backed images and drops unknown sources", () => {
    const artifacts = new Map([["run/images/chart.png", "https://signed.example/chart.png"]]);
    render(
      <SurveyMarkdown
        markdown={"![Evidence](images/chart.png)\n\n![Remote](https://tracker.example/pixel.png)"}
        imageArtifacts={artifacts}
      />,
    );
    expect(screen.getByRole("img", { name: "Evidence" })).toHaveAttribute(
      "src",
      "https://signed.example/chart.png",
    );
    expect(screen.queryByRole("img", { name: "Remote" })).not.toBeInTheDocument();
  });

  it("removes dangerous link protocols", () => {
    render(<SurveyMarkdown markdown={"[Unsafe](javascript:alert(1))"} />);
    expect(screen.getByText("Unsafe").closest("a")).toHaveAttribute("href", "");
  });

  it("renders links as non-interactive text in a card preview", () => {
    render(
      <SurveyMarkdown markdown={"[Read the study](https://arxiv.org/abs/2401.12345)"} preview />,
    );
    expect(screen.getByText("Read the study").closest("a")).toBeNull();
  });

  it("does not expose internal assembly markers", () => {
    render(<SurveyMarkdown markdown={"# Report\n\nFinal paragraph.\n\n<!--M4-->"} />);
    expect(screen.queryByText("<!--M4-->")).not.toBeInTheDocument();
  });
});
