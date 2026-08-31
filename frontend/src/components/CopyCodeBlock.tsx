import { useEffect, useRef, useState } from "react";

import { styles } from "../styles/classes";

/* eslint-disable jsx-a11y/no-noninteractive-tabindex -- scrollable code needs keyboard focus. */

type CopyState = "idle" | "copied" | "error";

type CopyCodeBlockProps = {
  code: string;
  language: string;
};

export function CopyCodeBlock({ code, language }: CopyCodeBlockProps) {
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const resetTimer = useRef<number | undefined>(undefined);

  useEffect(
    () => () => {
      if (resetTimer.current !== undefined) window.clearTimeout(resetTimer.current);
    },
    [],
  );

  const copy = async () => {
    if (resetTimer.current !== undefined) window.clearTimeout(resetTimer.current);
    try {
      await navigator.clipboard.writeText(code);
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }
    resetTimer.current = window.setTimeout(() => setCopyState("idle"), 1800);
  };

  const label =
    copyState === "copied" ? "Copied" : copyState === "error" ? "Copy failed" : "Copy code";
  const accessibleLabel =
    language + " code example: " + code.split("\n").slice(0, 2).join(" ").trim();

  return (
    <div className={styles.codeBlock}>
      <div className={styles.codeBlockHeader}>
        <span>{language}</span>
        <button type="button" onClick={copy} aria-live="polite">
          {label}
        </button>
      </div>
      {/* A keyboard-focusable region lets users scroll long code samples on touch and keyboard devices. */}
      <div className={styles.codeScroll} role="region" tabIndex={0} aria-label={accessibleLabel}>
        <pre>
          <code>{code}</code>
        </pre>
      </div>
    </div>
  );
}
