import { useQuery } from "@tanstack/react-query";

import { capabilitiesApi } from "../../api/domain";
import { queryKeys } from "../../app/queryKeys";

export const CAPABILITIES_STALE_TIME = 60_000;

export function usePublicCapabilities() {
  return useQuery({
    queryKey: queryKeys.capabilities,
    queryFn: capabilitiesApi.get,
    staleTime: CAPABILITIES_STALE_TIME,
    retry: false,
    refetchOnWindowFocus: true,
  });
}

export function availableSearchModes(capabilities?: {
  search_modes?: readonly string[];
}): ("standard" | "thorough")[] {
  return capabilities?.search_modes?.includes("thorough") ? ["standard", "thorough"] : ["standard"];
}
