import type { SurveyArtifact, SurveyProgress, SurveySummary } from "../../api/types";
import { formatElapsed, formatRelativeTime } from "../../i18n/format";
import type { AppLocale } from "../../i18n/I18nProvider";

export const SURVEY_POLL_INTERVAL = 5_000;
export const REPORT_PATH = "run/08_survey.md";

export const activeSurveyStages = new Set<SurveyProgress["stage"]>([
  "drafting",
  "waiting_for_draft",
  "waiting_for_execution",
  "planning",
  "discovering",
  "reviewing_evidence",
  "structuring_report",
  "writing_report",
  "finalizing",
  "cancelling",
  "saving_results",
]);

const stageLabels: Record<SurveyProgress["stage"], string> = {
  drafting: "Refining research requirements",
  waiting_for_draft: "Waiting to refine",
  waiting_for_execution: "Waiting to begin",
  planning: "Planning",
  discovering: "Discovering papers",
  reviewing_evidence: "Reviewing evidence",
  structuring_report: "Structuring report",
  writing_report: "Writing report",
  finalizing: "Finalizing",
  cancelling: "Cancelling",
  saving_results: "Saving results",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export function surveyStageLabel(stage: SurveyProgress["stage"]): string {
  return stageLabels[stage];
}

export function surveyTitle(title: string | null | undefined, initialRequest: string): string {
  if (title?.trim()) return title.trim();
  const fallback =
    initialRequest
      .split("\n")
      .find((line) => line.trim())
      ?.trim() || "Untitled survey";
  return fallback.length <= 96 ? fallback : `${fallback.slice(0, 95).trimEnd()}…`;
}

export function queueAhead(progress: SurveyProgress): number {
  return Math.max(0, (progress.queue?.position ?? 1) - 1);
}

export function queueDescription(progress: SurveyProgress, locale: AppLocale): string {
  const queue = progress.queue;
  if (!queue) return surveyStageLabel(progress.stage);
  const ahead = queueAhead(progress);
  const subject = queue.kind === "draft" ? "request" : "survey";
  const position =
    ahead === 0 ? "Next in queue" : `${ahead} ${subject}${ahead === 1 ? "" : "s"} ahead`;
  return `${surveyStageLabel(progress.stage)}  ·  ${position}  ·  Queued ${formatRelativeTime(queue.queued_at, locale)}`;
}

export function runningDescription(progress: SurveyProgress, locale: AppLocale): string {
  const parts = [surveyStageLabel(progress.stage)];
  if (progress.step > 0) parts.push(`Stage ${progress.step} of ${progress.total_steps}`);
  if (progress.elapsed_seconds > 0)
    parts.push(`Running for ${formatElapsed(progress.elapsed_seconds)}`);
  parts.push(`Last active ${formatRelativeTime(progress.last_activity_at, locale)}`);
  return parts.join("  ·  ");
}

export function shouldPollSummaries(items: SurveySummary[]): boolean {
  return items.some((item) => activeSurveyStages.has(item.progress.stage));
}

function normalizeArtifactPath(path: string): string | null {
  const parts: string[] = [];
  for (const part of path.replaceAll("\\", "/").split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (!parts.length) return null;
      parts.pop();
      continue;
    }
    parts.push(part);
  }
  return parts.join("/");
}

export function artifactUrlMap(items: SurveyArtifact[]): Map<string, string> {
  return new Map(items.map((item) => [item.path, item.download_url]));
}

export function resolveReportImage(url: string, artifacts: Map<string, string>): string | null {
  if (/^[a-z][a-z0-9+.-]*:/i.test(url) || url.startsWith("//") || url.startsWith("/")) return null;
  const clean = url.split(/[?#]/, 1)[0] ?? "";
  const normalized = normalizeArtifactPath(`run/${clean}`);
  return normalized ? (artifacts.get(normalized) ?? null) : null;
}

export function markdownFilename(title: string): string {
  const safe = title
    .normalize("NFKD")
    .replaceAll(/[^a-zA-Z0-9\p{L}\p{N}]+/gu, "-")
    .replaceAll(/^-+|-+$/g, "")
    .slice(0, 90);
  return `${safe || "scholight-survey"}.md`;
}

export function archiveFilename(title: string): string {
  return markdownFilename(title).replace(/\.md$/, ".zip");
}
