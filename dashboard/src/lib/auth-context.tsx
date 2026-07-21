"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useRouter, usePathname } from "next/navigation";
import { api, clearToken, isTokenValid, setToken } from "./api";

interface AuthState {
  ready: boolean;
  authenticated: boolean;
  login: (password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [ready, setReady] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);

  // Mount: check if we already have a valid token
  useEffect(() => {
    setAuthenticated(isTokenValid());
    setReady(true);
  }, []);

  // Token can die mid-session (TTL expiry or a 401 from any API call) --
  // api.ts clears localStorage but has no router access, so it dispatches
  // this event instead of us silently polling a dead token forever.
  useEffect(() => {
    const onInvalidated = () => setAuthenticated(false);
    window.addEventListener("auth:invalidated", onInvalidated);
    return () => window.removeEventListener("auth:invalidated", onInvalidated);
  }, []);

  // Bounce to /login whenever we go from authed → not
  useEffect(() => {
    if (!ready) return;
    if (!authenticated && !pathname.startsWith("/login")) {
      router.replace("/login");
    }
    if (authenticated && pathname === "/login") {
      router.replace("/");
    }
  }, [ready, authenticated, pathname, router]);

  const login = useCallback(
    async (password: string) => {
      const res = await api.login(password);
      setToken(res.token, res.expires_in_s);
      setAuthenticated(true);
    },
    [],
  );

  const logout = useCallback(() => {
    clearToken();
    setAuthenticated(false);
  }, []);

  const value = useMemo(
    () => ({ ready, authenticated, login, logout }),
    [ready, authenticated, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
