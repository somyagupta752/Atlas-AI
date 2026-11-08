import { Link, NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import ProtectedRoute from "./auth/ProtectedRoute";
import Overview from "./pages/Overview";
import StartEvaluation from "./pages/StartEvaluation";
import EvaluationDetail from "./pages/EvaluationDetail";
import Compare from "./pages/Compare";
import CaseInvestigation from "./pages/CaseInvestigation";
import Login from "./pages/Login";
import Register from "./pages/Register";
import ResetPassword from "./pages/ResetPassword";
import Repositories from "./pages/Repositories";
import RepositoryDetail from "./pages/RepositoryDetail";

function UserMenu() {
  const { status, user, logout, logoutEverywhere } = useAuth();
  const navigate = useNavigate();
  if (status !== "authenticated") {
    return status === "loading" ? null : <span><Link to="/login">Sign in</Link></span>;
  }
  return (
    <span className="user">
      <span title="Signed in as">{user.email}</span>
      <button className="secondary" onClick={async () => { await logout(); navigate("/login", { replace: true }); }}>
        Sign out
      </button>
      <button className="secondary" title="End your session on every device, immediately"
        onClick={async () => { await logoutEverywhere(); navigate("/login", { replace: true }); }}>
        Sign out everywhere
      </button>
    </span>
  );
}

// Page routes must never share a path with an API endpoint: both the production nginx and the dev/preview proxy forward
// /auth, /evaluations, /repositories and /debug to the API, so a page at /repositories would show JSON after a reload.
// (Hence /repos for the repositories pages. tests/deploy/test_compose.py checks that no page route collides.)
const guard = (page) => <ProtectedRoute>{page}</ProtectedRoute>;

export default function App() {
  const { status } = useAuth();
  return (
    <>
      <header className="top">
        <strong>Atlas AI · Evaluation Lab</strong>
        {status === "authenticated" && (
          <nav>
            <NavLink to="/" end>Overview</NavLink>
            <NavLink to="/repos">Repositories</NavLink>
            <NavLink to="/start">Start evaluation</NavLink>
            <NavLink to="/compare">Compare</NavLink>
          </nav>
        )}
        <UserMenu />
      </header>
      <main>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />
          <Route path="/reset-password" element={<ResetPassword />} />
          <Route path="/" element={guard(<Overview />)} />
          <Route path="/repos" element={guard(<Repositories />)} />
          <Route path="/repos/:id" element={guard(<RepositoryDetail />)} />
          <Route path="/start" element={guard(<StartEvaluation />)} />
          <Route path="/runs/:id" element={guard(<EvaluationDetail />)} />
          <Route path="/runs/:id/cases/:caseId" element={guard(<CaseInvestigation />)} />
          <Route path="/compare" element={guard(<Compare />)} />
          <Route path="*" element={<p className="state">Page not found.</p>} />
        </Routes>
      </main>
    </>
  );
}
