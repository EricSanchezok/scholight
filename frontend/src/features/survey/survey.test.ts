import { describe, expect, it } from "vitest";

import type { SurveyArtifact, SurveyProgress } from "../../api/types";
import {
  archiveFilename,
  artifactUrlMap,
  markdownFilename,
  queueAhead,
  resolveReportImage,
  surveyStageLabel,
  surveyTitle,
} from "./survey";

const queued: SurveyProgress = {
  survey_id: "00000000-0000-0000-0000-000000000001",
  status: "queued",
  stage: "waiting_for_execution",
  percent: 0,
  step: 0,
  total_steps: 7,
  elapsed_seconds: 0,
  started_at: null,
  finished_at: null,
  last_activity_at: "2026-07-31T10:00:00Z",
  queue: {
    kind: "survey",
    position: 3,
    queued_at: "2026-07-31T10:00:00Z",
    running_slots: 1,
    max_slots: 2,
  },
};

describe("Survey presentation helpers", () => {
  it("converts a one-based queue position into the number of requests ahead", () => {
    expect(queueAhead(queued)).toBe(2);
  });

  it("uses public stage names rather than internal pipeline components", () => {
    expect(surveyStageLabel("reviewing_evidence")).toBe("Reviewing evidence");
  });

  it("resolves only relative report images present in the artifact manifest", () => {
    const item: SurveyArtifact = {
      path: "run/images/evidence.png",
      size: 10,
      sha256: "abc",
      content_type: "image/png",
      download_url: "https://signed.example/evidence.png",
    };
    const artifacts = artifactUrlMap([item]);
    expect(resolveReportImage("images/evidence.png", artifacts)).toBe(item.download_url);
    expect(resolveReportImage("../../secret.png", artifacts)).toBeNull();
    expect(resolveReportImage("https://tracker.example/pixel.png", artifacts)).toBeNull();
  });

  it("creates a filesystem-safe Markdown filename", () => {
    expect(markdownFilename("AI & scientific work: 2026")).toBe("AI-scientific-work-2026.md");
  });

  it("creates a filesystem-safe report package filename", () => {
    expect(archiveFilename("AI & scientific work: 2026")).toBe("AI-scientific-work-2026.zip");
  });

  it("prefers the generated survey title", () => {
    expect(surveyTitle("Reasoning compression strategies", "A much longer request")).toBe(
      "Reasoning compression strategies",
    );
  });

  it("keeps a bounded request fallback while title generation is unavailable", () => {
    expect(surveyTitle(null, "x".repeat(120))).toHaveLength(96);
  });
});
