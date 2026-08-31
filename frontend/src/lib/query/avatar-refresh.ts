const FIFTEEN_MINUTES_MS = 15 * 60_000;
const REFRESH_BUFFER_MS = 60_000;
const MINIMUM_REFRESH_MS = 30_000;

type ExpiringAvatar = { expires_at: string };

export function nextAvatarRefreshInterval(
  avatars: Array<ExpiringAvatar | null | undefined>,
  now = Date.now(),
): number {
  const expirations = avatars
    .map((avatar) => (avatar ? Date.parse(avatar.expires_at) : Number.NaN))
    .filter(Number.isFinite);
  if (!expirations.length) return FIFTEEN_MINUTES_MS;
  return Math.max(MINIMUM_REFRESH_MS, Math.min(...expirations) - now - REFRESH_BUFFER_MS);
}
