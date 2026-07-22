import { Navigate, Outlet, useLocation } from "react-router-dom";

import { LoadingScreen } from "../components/LoadingScreen";
import { useAuth } from "./AuthProvider";

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

export function safeReturnTo(value: string | null): string {
  return value && /^\/(?!\/)/.test(value) ? value : "/";
}
