import { createContext, useContext } from "react";

import type { AdminCapabilities, LoginRequest, UserProfile } from "../api/types";

export type AuthStatus = "checking" | "anonymous" | "authenticated";

export interface AuthContextValue {
  status: AuthStatus;
  user: UserProfile | null;
  adminCapabilities: AdminCapabilities;
  login: (credentials: LoginRequest) => Promise<void>;
  logout: () => Promise<void>;
  refreshProfile: () => Promise<UserProfile>;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
