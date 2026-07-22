import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ThemeProvider, useTheme } from "./ThemeProvider";

function Probe() {
  const { theme } = useTheme();
  return <span>{theme}</span>;
}

describe("ThemeProvider", () => {
  it("installs the registered theme on the document root", () => {
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );

    expect(screen.getByText("light")).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.documentElement.style.colorScheme).toBe("light");
  });
});
