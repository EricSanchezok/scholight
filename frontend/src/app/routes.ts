import type { AdminCapabilities } from "../api/types";

export const routes = {
  home: { path: "/", segment: "" },
  search: { path: "/search", segment: "search" },
  docs: { path: "/docs", segment: "docs" },
  survey: { path: "/survey", segment: "survey" },
  surveyDraft: { path: "/survey/:surveyId/draft", segment: "survey/:surveyId/draft" },
  surveyReport: { path: "/survey/:surveyId/report", segment: "survey/:surveyId/report" },
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
  adminOverview: { path: "/admin", segment: "admin" },
  quotaAdmin: { path: "/admin/quotas", segment: "admin/quotas" },
  adminOperations: { path: "/admin/operations", segment: "admin/operations" },
  notFound: { path: "*", segment: "*" },
} as const;

export const surveyDraftPath = (surveyId: string) => `/survey/${surveyId}/draft`;
export const surveyReportPath = (surveyId: string) => `/survey/${surveyId}/report`;

export const accountRoutes = [
  { id: "usage", ...routes.usage },
  { id: "accessKeys", ...routes.accessKeys },
  { id: "history", ...routes.history },
  { id: "account", ...routes.account },
  {
    id: "adminOverview",
    requiredCapability: "can_view_analytics",
    ...routes.adminOverview,
  },
  {
    id: "quotaAdmin",
    requiredCapability: "can_manage_quotas",
    ...routes.quotaAdmin,
  },
  {
    id: "adminOperations",
    requiredCapability: "can_view_operations",
    ...routes.adminOperations,
  },
] as const;

export type AccountRoute = (typeof accountRoutes)[number];
export type AccountRouteId = AccountRoute["id"];
export type AccountDestination = AccountRoute["path"];

export function accountRouteFor(pathname: string): AccountRoute | undefined {
  return accountRoutes.find((route) => route.path === pathname);
}

export const emptyAdminCapabilities: AdminCapabilities = {
  can_manage_quotas: false,
  can_view_analytics: false,
  can_view_operations: false,
};

export function visibleAccountRoutes(capabilities: AdminCapabilities): readonly AccountRoute[] {
  return accountRoutes.filter(
    (route) => !("requiredCapability" in route) || capabilities[route.requiredCapability],
  );
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
