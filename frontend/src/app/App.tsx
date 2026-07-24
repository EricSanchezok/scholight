import { QueryClientProvider } from "@tanstack/react-query";
import { lazy } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import { AuthProvider } from "../auth/AuthProvider";
import { AdminRoute, AnonymousOnlyRoute, ProtectedRoute } from "../auth/routes";
import { SiteHeader } from "../components/SiteHeader";
import {
  CheckEmailPage,
  ForgotPasswordPage,
  LoginPage,
  RegisterPage,
  ResetPasswordPage,
  VerifyEmailPage,
} from "../pages/AuthPages";
import { DocsPage } from "../pages/DocsPage";
import { HomePage } from "../pages/HomePage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { SearchPage } from "../pages/SearchPage";

import { AnimatedOutlet, ScholightMotionProvider } from "./motion";
import { privateRouteLoaders } from "./privateRoutes";
import { queryClient } from "./queryClient";
import { routes } from "./routes";

const UsagePage = lazy(privateRouteLoaders[routes.usage.path]);
const AccessKeysPage = lazy(privateRouteLoaders[routes.accessKeys.path]);
const HistoryPage = lazy(privateRouteLoaders[routes.history.path]);
const AccountPage = lazy(privateRouteLoaders[routes.account.path]);
const AdminOverviewPage = lazy(privateRouteLoaders[routes.adminOverview.path]);
const QuotaAdminPage = lazy(privateRouteLoaders[routes.quotaAdmin.path]);
const AdminOperationsPage = lazy(privateRouteLoaders[routes.adminOperations.path]);

function SiteLayout() {
  return (
    <>
      <SiteHeader />
      <AnimatedOutlet />
    </>
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ScholightMotionProvider>
        <BrowserRouter>
          <AuthProvider>
            <Routes>
              <Route element={<SiteLayout />}>
                <Route index element={<HomePage />} />
                <Route path={routes.search.segment} element={<SearchPage />} />
                <Route path={routes.docs.segment} element={<DocsPage />} />
                <Route element={<ProtectedRoute />}>
                  <Route path={routes.history.segment} element={<HistoryPage />} />
                  <Route path={routes.usage.segment} element={<UsagePage />} />
                  <Route path={routes.accessKeys.segment} element={<AccessKeysPage />} />
                  <Route path={routes.account.segment} element={<AccountPage />} />
                  <Route element={<AdminRoute capability="can_view_analytics" />}>
                    <Route path={routes.adminOverview.segment} element={<AdminOverviewPage />} />
                  </Route>
                  <Route element={<AdminRoute capability="can_manage_quotas" />}>
                    <Route path={routes.quotaAdmin.segment} element={<QuotaAdminPage />} />
                  </Route>
                  <Route element={<AdminRoute capability="can_view_operations" />}>
                    <Route
                      path={routes.adminOperations.segment}
                      element={<AdminOperationsPage />}
                    />
                  </Route>
                </Route>
                <Route path={routes.notFound.segment} element={<NotFoundPage />} />
              </Route>
              <Route element={<AnonymousOnlyRoute />}>
                <Route path={routes.login.segment} element={<LoginPage />} />
                <Route path={routes.register.segment} element={<RegisterPage />} />
              </Route>
              <Route path={routes.checkEmail.segment} element={<CheckEmailPage />} />
              <Route path={routes.verifyEmail.segment} element={<VerifyEmailPage />} />
              <Route path={routes.forgotPassword.segment} element={<ForgotPasswordPage />} />
              <Route path={routes.resetPassword.segment} element={<ResetPasswordPage />} />
            </Routes>
          </AuthProvider>
        </BrowserRouter>
      </ScholightMotionProvider>
    </QueryClientProvider>
  );
}
