export const runtimeConfig = {
  apiBasePath: "/api",
} as const;

export function apiPath(path: `/${string}`): string {
  return `${runtimeConfig.apiBasePath}${path}`;
}
