import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";

import { accountApi, adminApi, authApi } from "../api/domain";
import type { LoginRequest, UserProfile } from "../api/types";
import { queryKeys } from "../app/queryKeys";
import { AuthContext, type AuthStatus } from "./context";
import {
  clearSession,
  establishSession,
  refreshAccessToken,
  subscribeToSessionChanges,
} from "./session";

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<AuthStatus>("checking");
  const [user, setUser] = useState<UserProfile | null>(null);
  const [canManageQuotas, setCanManageQuotas] = useState(false);
  const loadProfile = useCallback(async () => {
    const profile = await accountApi.profile();
    const capabilities = await adminApi.capabilities().catch(() => ({ can_manage_quotas: false }));
    setUser(profile);
    setCanManageQuotas(capabilities.can_manage_quotas);
    setStatus("authenticated");
    return profile;
  }, []);

  useEffect(() => {
    let active = true;
    const restore = async () => {
      try {
        await refreshAccessToken();
        const profile = await accountApi.profile();
        const capabilities = await adminApi
          .capabilities()
          .catch(() => ({ can_manage_quotas: false }));
        if (active) {
          setUser(profile);
          setCanManageQuotas(capabilities.can_manage_quotas);
          setStatus("authenticated");
        }
      } catch {
        if (active) {
          clearSession(false);
          setUser(null);
          setCanManageQuotas(false);
          setStatus("anonymous");
        }
      }
    };

    void restore();
    const unsubscribe = subscribeToSessionChanges(() => {
      setStatus("checking");
      void refreshAccessToken()
        .then(() => loadProfile())
        .catch(() => {
          clearSession(false);
          setUser(null);
          setCanManageQuotas(false);
          setStatus("anonymous");
          queryClient.removeQueries({ queryKey: queryKeys.privateRoot });
        });
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
      queryClient.removeQueries({ queryKey: queryKeys.privateRoot });
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
      setCanManageQuotas(false);
      setStatus("anonymous");
      queryClient.removeQueries({ queryKey: queryKeys.privateRoot });
    }
  }, [queryClient]);

  const value = useMemo(
    () => ({ status, user, canManageQuotas, login, logout, refreshProfile: loadProfile }),
    [status, user, canManageQuotas, login, logout, loadProfile],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
