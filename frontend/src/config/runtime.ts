export const runtimeConfig = {
  apiBasePath: "/api",
} as const;

export function apiPath(path: `/${string}`): string {
  return `${runtimeConfig.apiBasePath}${path}`;
}

export type DeploymentUrls = {
  web: string;
  api: string;
  search: string;
  mcp: string;
  openapi: string;
  interactiveApi: string;
};

export function buildDeploymentUrls(
  browserOrigin: string,
  apiBasePath: string = runtimeConfig.apiBasePath,
): DeploymentUrls {
  const web = new URL(browserOrigin).origin;
  const normalizedApiPath = `/${apiBasePath.replace(/^\/+|\/+$/g, "")}`;
  const api = new URL(normalizedApiPath, `${web}/`).toString().replace(/\/$/, "");

  return {
    web,
    api,
    search: `${api}/search`,
    mcp: `${api}/mcp`,
    openapi: `${api}/openapi.json`,
    interactiveApi: `${api}/docs`,
  };
}
