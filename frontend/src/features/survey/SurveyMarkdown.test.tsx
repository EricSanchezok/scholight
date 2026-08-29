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

  it("renders inline math with KaTeX", () => {
    const { container } = render(<SurveyMarkdown markdown={"Mass-energy: $E=mc^2$."} />);
    expect(container.querySelector(".katex")).toBeInTheDocument();
    expect(container.querySelector(".katex-display")).toBeNull();
  });

  it("renders block math as a KaTeX display block", () => {
    const { container } = render(<SurveyMarkdown markdown={"$$\n\\int_0^1 x\\,dx\n$$"} />);
    expect(container.querySelector(".katex-display .katex")).toBeInTheDocument();
  });

  it("renders GFM tables and math in the same document", () => {
    const { container } = render(
      <SurveyMarkdown
        markdown={"| Metric | Formula |\n| --- | --- |\n| Entropy | $H = -\\sum p \\log p$ |"}
      />,
    );
    expect(screen.getByRole("table").querySelector("thead")).toBeInTheDocument();
    expect(container.querySelector(".katex")).toBeInTheDocument();
  });

  it("renders dollar-free content without KaTeX markup", () => {
    const { container } = render(<SurveyMarkdown markdown={"# Report\n\nPlain paragraph."} />);
    expect(screen.getByRole("heading", { name: "Report" })).toBeInTheDocument();
    expect(container.querySelector(".katex")).toBeNull();
  });

  it("renders math in card previews", () => {
    const { container } = render(<SurveyMarkdown markdown={"$E=mc^2$"} preview />);
    expect(container.querySelector(".katex")).toBeInTheDocument();
  });

  it("does not treat dollar signs inside code spans as math", () => {
    const { container } = render(<SurveyMarkdown markdown={"Run `$x$` in the shell."} />);
    expect(container.querySelector("code")).toHaveTextContent("$x$");
    expect(container.querySelector(".katex")).toBeNull();
  });
});
