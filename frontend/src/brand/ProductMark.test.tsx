import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ProductMark } from "./ProductMark";

describe("ProductMark", () => {
  it("uses only unframed portrait assets at the requested size", () => {
    render(<ProductMark size={96} />);

    const image = screen.getByRole("img", { name: "Scholight lynx mark" });
    expect(image).toHaveAttribute("src", "/brand/scholight-lynx-portrait-512.png");
    expect(image).toHaveAttribute("width", "96");
    expect(image).toHaveAttribute("height", "96");
    expect(image.getAttribute("srcset")).toContain("64w");
    expect(image.getAttribute("srcset")).toContain("512w");
    expect(image.getAttribute("srcset")).not.toContain("/brand/icons/");
  });

  it("keeps decorative placements out of the accessibility tree", () => {
    render(<ProductMark decorative size={64} />);

    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(document.querySelector("img")).toHaveAttribute("aria-hidden", "true");
  });
});
