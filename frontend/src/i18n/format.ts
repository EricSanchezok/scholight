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
