import type { AppLocale } from "./I18nProvider";

export const defaultLocale: AppLocale = "en";

export function formatNumber(value: number, locale: AppLocale = defaultLocale): string {
  return new Intl.NumberFormat(locale).format(value);
}

export function formatCompactNumber(value: number, locale: AppLocale = defaultLocale): string {
  return new Intl.NumberFormat(locale, {
    notation: Math.abs(value) < 1000 ? "standard" : "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

export function formatCalendarDate(
  value: string | number | Date,
  locale: AppLocale = defaultLocale,
): string {
  return new Intl.DateTimeFormat(locale, {
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(new Date(value));
}

export function formatCompactDateTime(
  value: string | number | Date,
  locale: AppLocale = defaultLocale,
): string {
  return new Intl.DateTimeFormat(locale, {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function formatFullDateTime(
  value: string | number | Date,
  locale: AppLocale = defaultLocale,
): string {
  return new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function formatTime(
  value: string | number | Date,
  locale: AppLocale = defaultLocale,
): string {
  return new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit" }).format(
    new Date(value),
  );
}

export function formatUtcTime(
  value: string | number | Date,
  locale: AppLocale = defaultLocale,
): string {
  return new Intl.DateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: "UTC",
    timeZoneName: "short",
  }).format(new Date(value));
}

export function formatUtcDay(
  value: string | number | Date,
  locale: AppLocale = defaultLocale,
): string {
  return new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  }).format(new Date(value));
}

export function formatResearchDate(
  value: string | number | Date,
  style: "long" | "short" = "long",
  locale: AppLocale = defaultLocale,
): string {
  return new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: style === "long" ? "long" : "short",
    day: "numeric",
  }).format(new Date(value));
}

export function formatRelativeTime(
  value: string | number | Date,
  locale: AppLocale = defaultLocale,
  now = Date.now(),
): string {
  const seconds = Math.round((new Date(value).getTime() - now) / 1000);
  const absolute = Math.abs(seconds);
  const [amount, unit]: [number, Intl.RelativeTimeFormatUnit] =
    absolute < 60
      ? [seconds, "second"]
      : absolute < 3600
        ? [Math.round(seconds / 60), "minute"]
        : absolute < 86400
          ? [Math.round(seconds / 3600), "hour"]
          : [Math.round(seconds / 86400), "day"];
  return new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(amount, unit);
}

export function formatElapsed(seconds: number): string {
  const safe = Math.max(0, Math.round(seconds));
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m`;
  return `${safe}s`;
}

export function formatDurationBetween(
  startedAt?: string | null,
  finishedAt?: string | null,
): string {
  if (!startedAt || !finishedAt) return "—";
  return formatElapsed((new Date(finishedAt).getTime() - new Date(startedAt).getTime()) / 1000);
}

export function formatReportDate(
  value: string | number | Date,
  locale: AppLocale = defaultLocale,
): string {
  const date = new Date(value);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return `Today, ${formatTime(date, locale)}`;
  return formatUtcDay(date, locale);
}
