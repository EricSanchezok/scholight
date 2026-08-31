export type InstallInstructionKind = "android" | "ios" | "in-app";

export type InstallEnvironment = {
  kind: InstallInstructionKind;
  mobile: boolean;
  supported: boolean;
  standalone: boolean;
};

function isStandaloneDisplayMode(): boolean {
  if (typeof window === "undefined") return false;
  const standaloneMedia = window.matchMedia?.("(display-mode: standalone)").matches ?? false;
  return standaloneMedia || ("standalone" in navigator && navigator.standalone === true);
}

export function detectInstallEnvironment(
  userAgent = typeof navigator === "undefined" ? "" : navigator.userAgent,
  standalone = isStandaloneDisplayMode(),
  touchPoints = typeof navigator === "undefined" ? 0 : navigator.maxTouchPoints,
): InstallEnvironment {
  const ua = userAgent.toLowerCase();
  const inApp = /micromessenger|line\//.test(ua);
  const android = ua.includes("android");
  const ios =
    /iphone|ipad|ipod/.test(ua) ||
    (ua.includes("macintosh") && (ua.includes("mobile") || touchPoints > 1));

  if (inApp) {
    return { kind: "in-app", mobile: android || ios, supported: android || ios, standalone };
  }
  if (android) return { kind: "android", mobile: true, supported: true, standalone };
  if (ios) return { kind: "ios", mobile: true, supported: true, standalone };
  return { kind: "android", mobile: false, supported: false, standalone };
}

export function isStandaloneApp(): boolean {
  return isStandaloneDisplayMode();
}
