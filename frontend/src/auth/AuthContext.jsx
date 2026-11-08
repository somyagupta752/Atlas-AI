import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { onSessionLost, refreshSession, setToken } from "./session";

const AuthContext = createContext(null);

// status: "loading" while the stored refresh cookie is being exchanged for an access token on page load,
// then "authenticated" or "anonymous". The user object comes from the server, never from the browser's storage.
export function AuthProvider({ children }) {
  const [state, setState] = useState({ status: "loading", user: null, notice: null });

  useEffect(() => {
    let alive = true;
    refreshSession().then((result) => {
      if (!alive) return;
      if (result.user) setState({ status: "authenticated", user: result.user, notice: null });
      else if (result.error === "unavailable") setState({ status: "anonymous", user: null, notice: "The server cannot be reached." });
      else setState({ status: "anonymous", user: null, notice: null });
    });
    const stop = onSessionLost((reason) =>
      setState({ status: "anonymous", user: null, notice: reason === "expired" ? "Your session has expired. Sign in again." : null }),
    );
    return () => {
      alive = false;
      stop();
    };
  }, []);

  const adopt = useCallback((body) => {
    setToken(body.access_token);
    setState({ status: "authenticated", user: body.user, notice: null });
    return body.user;
  }, []);

  const value = useMemo(
    () => ({
      ...state,
      login: async (email, password) => adopt(await api.login(email, password)),
      register: async (email, password) => {
        await api.register(email, password);
        return adopt(await api.login(email, password));
      },
      logout: async () => {
        try {
          await api.logout();
        } catch {
          // The server may be unreachable; the local session must end regardless.
        }
        setToken(null);
        setState({ status: "anonymous", user: null, notice: null });
      },
      logoutEverywhere: async () => {
        await api.logoutEverywhere();
        setToken(null);
        setState({ status: "anonymous", user: null, notice: "Signed out on every device." });
      },
    }),
    [state, adopt],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}
