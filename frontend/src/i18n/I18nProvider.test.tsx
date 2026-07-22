import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { I18nProvider, useI18n } from "./I18nProvider";

function Probe() {
  const { locale, messages } = useI18n();
  return <span>{`${locale}:${messages.navigation.usage}`}</span>;
}

describe("I18nProvider", () => {
  it("installs the registered locale and typed catalog", () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );

    expect(screen.getByText("en:Usage & quota")).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en");
  });
});
