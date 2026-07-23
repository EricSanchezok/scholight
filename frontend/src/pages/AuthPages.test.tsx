import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authApi } from "../api/domain";
import { CheckEmailPage, RegisterPage } from "./AuthPages";

vi.mock("../api/domain", () => ({
  authApi: {
    register: vi.fn(),
    resendVerification: vi.fn(),
  },
}));

function Location() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}

describe("registration boundary", () => {
  beforeEach(() => {
    vi.mocked(authApi.register).mockReset();
    vi.mocked(authApi.resendVerification).mockReset();
  });

  it("always continues successful registration to the neutral check-email page", async () => {
    vi.mocked(authApi.register).mockResolvedValue({ message: "generic" });
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/register"]}>
        <RegisterPage />
        <Location />
      </MemoryRouter>,
    );

    await user.type(screen.getByRole("textbox", { name: "Email" }), "reader@example.com");
    await user.type(screen.getByPlaceholderText("Create a password"), "a-secure-password");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    expect(screen.getByTestId("location")).toHaveTextContent(
      "/check-email?email=reader%40example.com",
    );
  });

  it("does not claim that the submitted address is a new account", () => {
    render(
      <MemoryRouter initialEntries={["/check-email?email=reader%40example.com"]}>
        <CheckEmailPage />
      </MemoryRouter>,
    );

    expect(
      screen.getByText(/if this address is new or still awaiting verification/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
    expect(screen.getByRole("link", { name: "Use a different email" })).toHaveAttribute(
      "href",
      "/register",
    );
  });

  it("disables resend while pending and reports the same neutral success", async () => {
    let resolveRequest: ((value: { message: string }) => void) | undefined;
    vi.mocked(authApi.resendVerification).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRequest = resolve;
        }),
    );
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/check-email?email=reader%40example.com"]}>
        <CheckEmailPage />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Resend verification" }));
    expect(screen.getByRole("button", { name: "Sending…" })).toBeDisabled();
    resolveRequest?.({ message: "generic" });

    expect(
      await screen.findByText(
        "If the account is awaiting verification, a new link will arrive shortly.",
      ),
    ).toBeInTheDocument();
  });
});
