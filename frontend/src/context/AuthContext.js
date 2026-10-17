import { createContext, useContext, useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";

const AuthCtx = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null); // null = checking, false = anon, object = authed
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const me = await api.get("/auth/me");
      // Only an object can be a user. Anything else (e.g. an HTML page
      // accepted as data when the API base is misconfigured) means
      // "not signed in" — storing it would crash every consumer that
      // reads user fields and blank the app.
      setUser(me && typeof me === "object" && !Array.isArray(me) ? me : false);
    } catch {
      setUser(false);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const login = async (email, password, totpCode) => {
    await api.post("/auth/login", { email, password, totp_code: totpCode || "" });
    await refresh();
  };
  const register = async (body) => {
    await api.post("/auth/register", body);
    await refresh();
  };
  const logout = async () => {
    await api.post("/auth/logout", {});
    setUser(false);
  };
  const switchRole = async (role) => {
    await api.post("/auth/switch-role", { role });
    await refresh();
  };
  const switchWorkspace = async (tenant_id) => {
    await api.post("/auth/switch", { tenant_id });
    await refresh();
  };

  return (
    <AuthCtx.Provider value={{ user, loading, login, register, logout, switchRole, switchWorkspace, refresh }}>
      {children}
    </AuthCtx.Provider>
  );
}

export const useAuth = () => useContext(AuthCtx);
