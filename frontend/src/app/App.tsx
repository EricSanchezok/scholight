import { QueryClientProvider } from "@tanstack/react-query";
import { lazy } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import { AuthProvider } from "../auth/AuthProvider";
import { AnonymousOnlyRoute, ProtectedRoute } from "../auth/routes";
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

const UsagePage = lazy(privateRouteLoaders["/usage"]);
const AccessKeysPage = lazy(privateRouteLoaders["/access-keys"]);
const HistoryPage = lazy(privateRouteLoaders["/history"]);
const AccountPage = lazy(privateRouteLoaders["/account"]);

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
      </ScholightMotionProvider>
    </QueryClientProvider>
  );
}
