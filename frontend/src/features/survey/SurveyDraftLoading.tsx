import { useReducedMotion } from "motion/react";
import * as m from "motion/react-m";

import { skeletonPulseMotion } from "../../app/motion";
import { styles } from "../../styles/classes";

export function SurveyDraftLoading({ status }: { status: "queued" | "running" }) {
  const reduceMotion = useReducedMotion();
  const queued = status === "queued";
  const label = queued ? "Waiting to prepare research brief" : "Generating research brief";

  return (
    <div
      className={styles.surveyDraftLoading}
      role="status"
      aria-label={label}
      aria-live="polite"
      aria-atomic="true"
    >
      <span className="sr-only">{label}</span>
      <m.div
        className={styles.surveyDraftLoadingLines}
        aria-hidden="true"
        {...skeletonPulseMotion(reduceMotion)}
      >
        <span />
        <span />
        <span />
        <span />
        <span />
        <span />
        <span />
      </m.div>
      <p aria-hidden="true">
        {queued
          ? "Research is busy. Your draft will begin automatically."
          : "Shaping your research brief…"}
      </p>
    </div>
  );
}
