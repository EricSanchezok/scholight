import { describe, expect, it } from "vitest";

import { contentSwapMotion } from "./motion";

describe("contentSwapMotion", () => {
  it("does not move content during a state transition", () => {
    expect(contentSwapMotion.initial).toEqual({ opacity: 0.35 });
    expect(contentSwapMotion.animate).not.toHaveProperty("y");
  });
});
