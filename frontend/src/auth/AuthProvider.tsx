import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { accountApi, authApi } from "../api/domain";
import type { LoginRequest, UserProfile } from "../api/types";
import {
  clearSession,
  establishSession,
  hasRefreshToken,
  refreshAccessToken,
  subscribeToSessionChanges,
} from "./session";

type AuthStatus = "checking" | "anonymous" | "authenticated";

interface AuthContextValue {
  status: AuthStatus;
  user: UserProfile | null;
  login: (credentials: LoginRequest) => Promise<void>;
  logout: () => Promise<void>;
  refreshProfile: () => Promise<UserProfile>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<AuthStatus>(hasRefreshToken() ? "checking" : "anonymous");
  const [user, setUser] = useState<UserProfile | null>(null);
  const statusRef = useRef(status);
  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  const loadProfile = useCallback(async () => {
    const profile = await accountApi.profile();
    setUser(profile);
    setStatus("authenticated");
    return profile;
  }, []);

  useEffect(() => {
    let active = true;
    const restore = async () => {
      if (!hasRefreshToken()) {
        if (active) {
          setUser(null);
          setStatus("anonymous");
        }
        return;
      }
      try {
        await refreshAccessToken();
        const profile = await accountApi.profile();
        if (active) {
          setUser(profile);
          setStatus("authenticated");
        }
      } catch {
        if (active) {
          clearSession(false);
          setUser(null);
          setStatus("anonymous");
        }
      }
    };

    void restore();
    const unsubscribe = subscribeToSessionChanges(() => {
      if (!hasRefreshToken()) {
        setUser(null);
        setStatus("anonymous");
        queryClient.removeQueries({ queryKey: ["private"] });
      } else if (statusRef.current === "anonymous") {
        setStatus("checking");
        void refreshAccessToken()
          .then(() => loadProfile())
          .catch(() => {
            clearSession(false);
            setStatus("anonymous");
          });
      }
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, [loadProfile, queryClient]);

  const login = useCallback(
    async (credentials: LoginRequest) => {
      const tokens = await authApi.login(credentials);
      establishSession(tokens);
      await loadProfile();
      queryClient.removeQueries({ queryKey: ["private"] });
    },
    [loadProfile, queryClient],
  );

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      // Local logout is authoritative even if the server cannot be reached.
    } finally {
      clearSession();
      setUser(null);
      setStatus("anonymous");
      queryClient.removeQueries({ queryKey: ["private"] });
    }
  }, [queryClient]);

  const value = useMemo(
    () => ({ status, user, login, logout, refreshProfile: loadProfile }),
    [status, user, login, logout, loadProfile],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
