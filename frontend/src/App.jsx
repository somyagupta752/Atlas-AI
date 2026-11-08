import { useEffect, useState } from "react";
import { Link, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Icon } from "./ui";
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
  if (status !== "authenticated") return null;
  return (
    <div className="user">
      <div className="user-id">
        <span className="avatar" aria-hidden="true">{user.email.slice(0, 2).toUpperCase()}</span>
        <span title="Signed in as">{user.email}</span>
      </div>
      <div className="user-actions">
        <button className="secondary" onClick={async () => { await logout(); navigate("/login", { replace: true }); }}>
          Sign out
        </button>
        <button className="secondary" title="End your session on every device, immediately"
          onClick={async () => { await logoutEverywhere(); navigate("/login", { replace: true }); }}>
          Sign out everywhere
        </button>
      </div>
    </div>
  );
}

// Live system status from the API's readiness probe. Shown as pills, never hidden when something is wrong.
function SystemStatus() {
  const [state, setState] = useState({ tone: "warn", label: "checking…" });
  useEffect(() => {
    let alive = true;
    const check = async () => {
      let next;
      try {
        const response = await fetch("/ready", { credentials: "same-origin" });
        const body = await response.json().catch(() => null);
        const bad = body && body.checks ? Object.entries(body.checks).filter(([, v]) => v !== "ok" && !["local", "inline"].includes(v)).map(([k]) => k) : [];
        next = response.ok && bad.length === 0 ? { tone: "ok", label: "system nominal" } : { tone: "bad", label: bad.length ? `${bad.join(", ")} down` : "degraded" };
      } catch {
        next = { tone: "bad", label: "API unreachable" };
      }
      if (alive) setState(next);
    };
    check();
    const timer = setInterval(check, 30000);
    return () => { alive = false; clearInterval(timer); };
  }, []);
  return (
    <>
      <span className="pill"><Icon name="radio" size={14} /> live</span>
      <span className={`pill ${state.tone}`} title="API readiness"><span className="dot" />{state.label}</span>
    </>
  );
}

const NAV = [
  { label: "Workspace", items: [
    { to: "/", end: true, icon: "grid", text: "Overview", title: "Evaluations" },
    { to: "/repos", icon: "repo", text: "Repositories", title: "Repositories" },
  ] },
  { label: "Evaluation lab", items: [
    { to: "/start", icon: "play", text: "Start evaluation", title: "New evaluation" },
    { to: "/compare", icon: "compare", text: "Compare", title: "Compare" },
  ] },
];

function Sidebar() {
  return (
    <aside className="sidebar">
      <Link to="/" className="brand" aria-label="Atlas AI home">
        <span className="brand-mark"><Icon name="graph" size={20} /></span>
        <span><strong>ATLAS</strong><small>Evaluation lab</small></span>
      </Link>
      <nav aria-label="Main">
        {NAV.map((group) => (
          <div key={group.label}>
            <div className="nav-label">{group.label}</div>
            <div className="nav">
              {group.items.map((i) => (
                <NavLink key={i.to} to={i.to} end={i.end}><Icon name={i.icon} />{i.text}</NavLink>
              ))}
            </div>
          </div>
        ))}
      </nav>
      <div className="sidebar-foot"><UserMenu /></div>
    </aside>
  );
}

function sectionTitle(pathname) {
  const hit = NAV.flatMap((g) => g.items).filter((i) => (i.end ? pathname === i.to : pathname.startsWith(i.to)))[0];
  if (hit) return hit.title;
  return pathname.startsWith("/runs") ? "Evaluation run" : "Atlas";
}

// Page routes must never share a path with an API endpoint: both the production nginx and the dev/preview proxy forward
// /auth, /evaluations, /repositories and /debug to the API, so a page at /repositories would show JSON after a reload.
// (Hence /repos for the repositories pages. tests/deploy/test_compose.py checks that no page route collides.)
const guard = (page) => <ProtectedRoute>{page}</ProtectedRoute>;

const AUTH_PATHS = ["/login", "/register", "/reset-password"];

export default function App() {
  const { status } = useAuth();
  const { pathname } = useLocation();
  const routes = (
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
  );
  // Sign-in, registration and reset are centred cards with no navigation: there is nothing to navigate to yet.
  if (status !== "authenticated" && AUTH_PATHS.includes(pathname)) {
    return (
      <div className="auth-shell">
        <div className="auth-card">
          <div className="brand"><span className="brand-mark"><Icon name="graph" size={20} /></span>
            <span><strong>ATLAS</strong><small>Evaluation lab</small></span></div>
          {routes}
        </div>
      </div>
    );
  }
  return (
    <div className="shell">
      {status === "authenticated" ? <Sidebar /> : (
        <aside className="sidebar"><Link to="/login" className="brand"><span className="brand-mark"><Icon name="graph" size={20} /></span>
          <span><strong>ATLAS</strong><small>Evaluation lab</small></span></Link></aside>
      )}
      <div className="workspace">
        <header className="topbar">
          <span className="crumb"><Icon name="radio" size={15} /> {sectionTitle(pathname)}</span>
          <div className="topbar-right"><SystemStatus /></div>
        </header>
        <main>{routes}</main>
      </div>
    </div>
  );
}
