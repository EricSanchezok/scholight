import type { QueryClient } from "@tanstack/react-query";
import type { ComponentType } from "react";

import { accountApi, accessKeyApi, historyApi, usageApi } from "../api/domain";
import { productConfig } from "../config/product";
import { queryKeys } from "./queryKeys";
import { PRIVATE_STALE_TIME } from "./queryClient";
import { routes, type AccountDestination } from "./routes";

type RouteModule = { default: ComponentType };

export const privateRouteLoaders: Record<AccountDestination, () => Promise<RouteModule>> = {
  [routes.usage.path]: () =>
    import("../pages/UsagePage").then((module) => ({ default: module.UsagePage })),
  [routes.accessKeys.path]: () =>
    import("../pages/AccessKeysPage").then((module) => ({ default: module.AccessKeysPage })),
  [routes.history.path]: () =>
    import("../pages/HistoryPage").then((module) => ({ default: module.HistoryPage })),
  [routes.account.path]: () =>
    import("../pages/AccountPage").then((module) => ({ default: module.AccountPage })),
};

export function preloadPrivateRoutes(): void {
  Object.values(privateRouteLoaders).forEach((loader) => {
    void loader().catch(() => undefined);
  });
}

export async function prefetchPrivateDestination(
  destination: AccountDestination,
  queryClient: QueryClient,
): Promise<void> {
  void privateRouteLoaders[destination]().catch(() => undefined);
  const common = { staleTime: PRIVATE_STALE_TIME };

  if (destination === routes.usage.path) {
    await Promise.all([
      queryClient.prefetchQuery({
        ...common,
        queryKey: queryKeys.usageSummary,
        queryFn: usageApi.summary,
      }),
      queryClient.prefetchQuery({
        ...common,
        queryKey: queryKeys.usageVolume,
        queryFn: usageApi.volume,
      }),
      queryClient.prefetchQuery({
        ...common,
        queryKey: queryKeys.usageLatency,
        queryFn: usageApi.latency,
      }),
      queryClient.prefetchInfiniteQuery({
        ...common,
        queryKey: queryKeys.usageRecords,
        queryFn: ({ pageParam }) => usageApi.records(pageParam),
        initialPageParam: undefined as string | undefined,
      }),
    ]);
    return;
  }
  if (destination === routes.accessKeys.path) {
    await queryClient.prefetchQuery({
      ...common,
      queryKey: queryKeys.accessKeys,
      queryFn: accessKeyApi.list,
    });
    return;
  }
  if (destination === routes.history.path) {
    await queryClient.prefetchQuery({
      ...common,
      queryKey: queryKeys.history("", 1),
      queryFn: () => historyApi.list(productConfig.history.pageSize, 0),
    });
    return;
  }
  await Promise.all([
    queryClient.prefetchQuery({
      ...common,
      queryKey: queryKeys.profile,
      queryFn: accountApi.profile,
    }),
    queryClient.prefetchQuery({
      ...common,
      queryKey: queryKeys.sessions,
      queryFn: accountApi.sessions,
    }),
  ]);
}
