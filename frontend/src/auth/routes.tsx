import { Navigate, Outlet, useLocation } from "react-router-dom";

import { LoadingScreen } from "../components/LoadingScreen";
import { useAuth } from "./context";

export function ProtectedRoute() {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "checking") return <LoadingScreen />;
  if (status === "anonymous") {
    const returnTo = `${location.pathname}${location.search}`;
    return <Navigate to={`/login?returnTo=${encodeURIComponent(returnTo)}`} replace />;
  }
  return <Outlet />;
}

export function AnonymousOnlyRoute() {
  const { status } = useAuth();
  if (status === "checking") return <LoadingScreen />;
  return status === "authenticated" ? <Navigate to="/" replace /> : <Outlet />;
}
