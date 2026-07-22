export type ExpiryPreset = "keep" | "never" | "30" | "90" | "365";

export function expiryFromPreset(
  preset: Exclude<ExpiryPreset, "keep" | "never">,
  now = new Date(),
): string {
  const expiresAt = new Date(now);
  expiresAt.setUTCDate(expiresAt.getUTCDate() + Number(preset));
  return expiresAt.toISOString();
}

export function accessKeyStatus(
  key: { expires_at: string | null; revoked_at: string | null },
  now = new Date(),
): "active" | "expired" | "revoked" {
  if (key.revoked_at) return "revoked";
  if (key.expires_at && new Date(key.expires_at).getTime() <= now.getTime()) return "expired";
  return "active";
}

export function parseUserAgent(value: string | null): string {
  if (!value) return "Unknown browser/device";
  let browser = "Unknown browser";
  if (/Edg\//.test(value)) browser = "Edge";
  else if (/OPR\//.test(value)) browser = "Opera";
  else if (/Chrome\//.test(value) || /CriOS\//.test(value)) browser = "Chrome";
  else if (/Firefox\//.test(value) || /FxiOS\//.test(value)) browser = "Firefox";
  else if (/Safari\//.test(value)) browser = "Safari";

  let system = "Unknown device";
  if (/iPhone|iPad|iPod/.test(value)) system = "iOS";
  else if (/Android/.test(value)) system = "Android";
  else if (/Macintosh|Mac OS X/.test(value)) system = "macOS";
  else if (/Windows/.test(value)) system = "Windows";
  else if (/Linux/.test(value)) system = "Linux";
  return `${browser} on ${system}`;
}

export function sortSessions<T extends { current: boolean; last_seen_at: string | null }>(
  sessions: T[],
): T[] {
  return [...sessions].sort((left, right) => {
    if (left.current !== right.current) return left.current ? -1 : 1;
    return (right.last_seen_at ?? "").localeCompare(left.last_seen_at ?? "");
  });
}
