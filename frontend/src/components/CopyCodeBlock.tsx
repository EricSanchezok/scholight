import { useEffect, useRef, useState } from "react";

import { styles } from "../styles/classes";

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

  return (
    <div className={styles.codeBlock}>
      <div className={styles.codeBlockHeader}>
        <span>{language}</span>
        <button type="button" onClick={copy} aria-live="polite">
          {label}
        </button>
      </div>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  );
}
