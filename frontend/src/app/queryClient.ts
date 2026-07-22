import { QueryClient } from "@tanstack/react-query";

export const PRIVATE_STALE_TIME = 60_000;
export const PRIVATE_GC_TIME = 10 * 60_000;

export const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false } },
});

queryClient.setQueryDefaults(["private"], {
  staleTime: PRIVATE_STALE_TIME,
  gcTime: PRIVATE_GC_TIME,
  retry: false,
});
