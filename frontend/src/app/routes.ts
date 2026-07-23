export const routes = {
  home: { path: "/", segment: "" },
  search: { path: "/search", segment: "search" },
  docs: { path: "/docs", segment: "docs" },
  login: { path: "/login", segment: "login" },
  register: { path: "/register", segment: "register" },
  checkEmail: { path: "/check-email", segment: "check-email" },
  verifyEmail: { path: "/verify-email", segment: "verify-email" },
  forgotPassword: { path: "/forgot-password", segment: "forgot-password" },
  resetPassword: { path: "/reset-password", segment: "reset-password" },
  usage: { path: "/usage", segment: "usage" },
  accessKeys: { path: "/access-keys", segment: "access-keys" },
  history: { path: "/history", segment: "history" },
  account: { path: "/account", segment: "account" },
  quotaAdmin: { path: "/admin/quotas", segment: "admin/quotas" },
  notFound: { path: "*", segment: "*" },
} as const;

export const accountRoutes = [
  { id: "usage", adminOnly: false, ...routes.usage },
  { id: "accessKeys", adminOnly: false, ...routes.accessKeys },
  { id: "history", adminOnly: false, ...routes.history },
  { id: "account", adminOnly: false, ...routes.account },
  { id: "quotaAdmin", adminOnly: true, ...routes.quotaAdmin },
] as const;

export type AccountRoute = (typeof accountRoutes)[number];
export type AccountRouteId = AccountRoute["id"];
export type AccountDestination = AccountRoute["path"];

export function accountRouteFor(pathname: string): AccountRoute | undefined {
  return accountRoutes.find((route) => route.path === pathname);
}

export function visibleAccountRoutes(canManageQuotas: boolean): readonly AccountRoute[] {
  return accountRoutes.filter((route) => !route.adminOnly || canManageQuotas);
}

export function withQuery(
  path: string,
  values: Record<string, string | number | undefined>,
): string {
  const query = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined) query.set(key, String(value));
  });
  const serialized = query.toString();
  return serialized ? `${path}?${serialized}` : path;
}
