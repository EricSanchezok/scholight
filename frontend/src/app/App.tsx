import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import { BrowserRouter, Outlet, Route, Routes } from "react-router-dom";

import { AuthProvider } from "../auth/AuthProvider";
import { AnonymousOnlyRoute, ProtectedRoute } from "../auth/routes";
import { SiteHeader } from "../components/SiteHeader";
import { LoadingScreen } from "../components/LoadingScreen";
import { AccountPage } from "../pages/AccountPage";
import {
  CheckEmailPage,
  ForgotPasswordPage,
  LoginPage,
  RegisterPage,
  ResetPasswordPage,
  VerifyEmailPage,
} from "../pages/AuthPages";
import { DocsPage } from "../pages/DocsPage";
import { HistoryPage } from "../pages/HistoryPage";
import { HomePage } from "../pages/HomePage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { SearchPage } from "../pages/SearchPage";

const UsagePage = lazy(() =>
  import("../pages/UsagePage").then((module) => ({ default: module.UsagePage })),
);
const AccessKeysPage = lazy(() =>
  import("../pages/AccessKeysPage").then((module) => ({ default: module.AccessKeysPage })),
);

const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false } },
});

function SiteLayout() {
  return (
    <>
      <SiteHeader />
      <Suspense fallback={<LoadingScreen />}>
        <Outlet />
      </Suspense>
    </>
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <Routes>
            <Route element={<SiteLayout />}>
              <Route index element={<HomePage />} />
              <Route path="search" element={<SearchPage />} />
              <Route path="docs" element={<DocsPage />} />
              <Route element={<ProtectedRoute />}>
                <Route path="history" element={<HistoryPage />} />
                <Route path="usage" element={<UsagePage />} />
                <Route path="access-keys" element={<AccessKeysPage />} />
                <Route path="account" element={<AccountPage />} />
              </Route>
              <Route path="*" element={<NotFoundPage />} />
            </Route>
            <Route element={<AnonymousOnlyRoute />}>
              <Route path="login" element={<LoginPage />} />
              <Route path="register" element={<RegisterPage />} />
            </Route>
            <Route path="check-email" element={<CheckEmailPage />} />
            <Route path="verify-email" element={<VerifyEmailPage />} />
            <Route path="forgot-password" element={<ForgotPasswordPage />} />
            <Route path="reset-password" element={<ResetPasswordPage />} />
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
