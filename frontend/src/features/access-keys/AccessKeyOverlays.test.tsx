import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SecretDialog } from "./AccessKeyOverlays";

const secret = {
  id: "9",
  name: "literature-review",
  key: "sk_live_secret",
  prefix: "sk_live_",
  last4: "cret",
  scopes: ["search" as const],
  created_at: "2026-07-23T00:00:00Z",
  expires_at: null,
  last_used_at: null,
  revoked_at: null,
};

describe("SecretDialog", () => {
  it("changes the button and announces successful copy", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    render(<SecretDialog secret={secret} onDone={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Copy key" }));

    expect(writeText).toHaveBeenCalledWith(secret.key);
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Access key copied.");
  });

  it("selects the key and shows a visible message when copy fails", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
    render(<SecretDialog secret={secret} onDone={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Copy key" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Copy failed. The key is selected so you can copy it manually.",
    );
    expect(screen.getByRole("textbox", { name: "New access key" })).toHaveFocus();
  });
});
