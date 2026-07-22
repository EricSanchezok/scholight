import { Navigate, Outlet, useLocation } from "react-router-dom";

import { LoadingScreen } from "../components/LoadingScreen";
import { RouteSkeleton } from "../components/EditorialSkeleton";
import { routes, withQuery } from "../app/routes";
import { useAuth } from "./context";

export function ProtectedRoute() {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "checking") return <RouteSkeleton pathname={location.pathname} />;
  if (status === "anonymous") {
    const returnTo = `${location.pathname}${location.search}`;
    return <Navigate to={withQuery(routes.login.path, { returnTo })} replace />;
  }
  return <Outlet />;
}

export function AnonymousOnlyRoute() {
  const { status } = useAuth();
  if (status === "checking") return <LoadingScreen />;
  return status === "authenticated" ? <Navigate to={routes.home.path} replace /> : <Outlet />;
}
