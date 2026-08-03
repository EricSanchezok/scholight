export const runtimeConfig = {
  apiBasePath: "/api",
} as const;

export function apiPath(path: `/${string}`): string {
  return `${runtimeConfig.apiBasePath}${path}`;
}

export type DeploymentUrls = {
  search: string;
  extract: string;
  mcp: string;
};

export function buildDeploymentUrls(
  browserOrigin: string,
  apiBasePath: string = runtimeConfig.apiBasePath,
): DeploymentUrls {
  const web = new URL(browserOrigin).origin;
  const normalizedApiPath = `/${apiBasePath.replace(/^\/+|\/+$/g, "")}`;
  const api = new URL(normalizedApiPath, `${web}/`).toString().replace(/\/$/, "");

  return {
    search: `${api}/search`,
    extract: `${api}/extract`,
    mcp: `${api}/mcp`,
  };
}
