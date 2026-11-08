import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "./AuthContext";

// Wraps every page that needs a signed-in user. Unauthenticated visitors are sent to the login page, which sends them
// back afterwards (the target is validated by safeRedirect). Nothing inside is rendered, and so nothing is fetched,
// until the session is known.
export default function ProtectedRoute({ children }) {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "loading") return <p className="state" role="status">Checking your session…</p>;
  if (status === "anonymous") {
    return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />;
  }
  return children;
}
