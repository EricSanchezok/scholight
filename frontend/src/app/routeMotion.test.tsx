import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { describe, expect, it } from "vitest";

import globalStyles from "../styles/global.css?raw";
import { AnimatedOutlet, ScholightMotionProvider } from "./motion";

function RouteLayout() {
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => navigate("/second")}>
        Change route
      </button>
      <AnimatedOutlet />
    </>
  );
}

describe("route motion", () => {
  it("keeps the outgoing route content frozen while it exits", async () => {
    const user = userEvent.setup();
    render(
      <ScholightMotionProvider>
        <MemoryRouter initialEntries={["/first"]}>
          <Routes>
            <Route element={<RouteLayout />}>
              <Route path="/first" element={<main>First route</main>} />
              <Route path="/second" element={<main>Second route</main>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </ScholightMotionProvider>,
    );

    const firstRoute = screen.getByText("First route");
    expect(firstRoute).toBeInTheDocument();
    await waitFor(() => expect(firstRoute.closest(".routeScene")).toHaveStyle({ opacity: "1" }));
    await user.click(screen.getByRole("button", { name: "Change route" }));

    expect(screen.getByText("First route")).toBeInTheDocument();
    expect(screen.queryByText("Second route")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Second route")).toBeInTheDocument());
  });

  it("hides only the root scrollbar without disabling page scrolling", () => {
    expect(globalStyles).toContain("scrollbar-width: none");
    expect(globalStyles).toContain("html::-webkit-scrollbar");
  });
});
