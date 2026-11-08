import { useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { safeRedirect } from "../auth/safeRedirect";

export function describeAuthError(error) {
  if (error.status === 429) {
    return error.retryAfter ? `Too many attempts. Try again in ${error.retryAfter} seconds.` : "Too many attempts. Try again later.";
  }
  if (error.status === 0) return "The server cannot be reached.";
  return error.message;
}

export default function Login() {
  const { status, notice, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const target = safeRedirect(new URLSearchParams(location.search).get("next"));
  const [form, setForm] = useState({ email: "", password: "" });
  const [state, setState] = useState({ busy: false, error: null });

  if (status === "authenticated") return <Navigate to={target} replace />;

  async function submit(event) {
    event.preventDefault();
    setState({ busy: true, error: null });
    try {
      await login(form.email, form.password);
      navigate(target, { replace: true });
    } catch (error) {
      setState({ busy: false, error: describeAuthError(error) });
    }
  }

  return (
    <form onSubmit={submit} className="form narrow" aria-label="Sign in">
      <h1>Sign in</h1>
      {notice && <p className="state" role="status">{notice}</p>}
      <label>Email
        <input type="email" autoComplete="username" value={form.email} required
          onChange={(e) => setForm({ ...form, email: e.target.value })} />
      </label>
      <label>Password
        <input type="password" autoComplete="current-password" value={form.password} required
          onChange={(e) => setForm({ ...form, password: e.target.value })} />
      </label>
      {state.error && <div className="state error" role="alert">{state.error}</div>}
      <button type="submit" disabled={state.busy}>{state.busy ? "Signing in…" : "Sign in"}</button>
      <p><Link to="/register">Create an account</Link> · <Link to="/reset-password">Forgot your password?</Link></p>
    </form>
  );
}
