import { Navigate, Outlet, useLocation } from "react-router-dom";

import { routes } from "../../app/routes";
import { RouteSkeleton } from "../../components/EditorialSkeleton";
import { usePublicCapabilities } from "./usePublicCapabilities";

export function SurveyAvailabilityRoute() {
  const location = useLocation();
  const capabilities = usePublicCapabilities();

  if (capabilities.isPending) {
    return <RouteSkeleton pathname={location.pathname} />;
  }
  if (capabilities.data?.survey !== "all") {
    return <Navigate to={routes.home.path} replace />;
  }
  return <Outlet />;
}
