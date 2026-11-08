import { useState } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { describeAuthError } from "./Login";

const MIN_LENGTH = 12;

export default function Register() {
  const { status, register } = useAuth();
  const navigate = useNavigate();
  const [form, setForm] = useState({ email: "", password: "", confirm: "" });
  const [state, setState] = useState({ busy: false, error: null });

  if (status === "authenticated") return <Navigate to="/" replace />;

  const mismatch = form.confirm !== "" && form.confirm !== form.password;
  const tooShort = form.password !== "" && form.password.length < MIN_LENGTH;

  async function submit(event) {
    event.preventDefault();
    if (mismatch || tooShort) return;
    setState({ busy: true, error: null });
    try {
      await register(form.email, form.password);
      navigate("/repos", { replace: true });
    } catch (error) {
      setState({ busy: false, error: error.status === 403 ? "Registration is closed." : describeAuthError(error) });
    }
  }

  return (
    <form onSubmit={submit} className="form narrow" aria-label="Create an account">
      <h1>Create an account</h1>
      <label>Email
        <input type="email" autoComplete="username" value={form.email} required
          onChange={(e) => setForm({ ...form, email: e.target.value })} />
      </label>
      <label>Password (at least {MIN_LENGTH} characters)
        <input type="password" autoComplete="new-password" value={form.password} required minLength={MIN_LENGTH}
          onChange={(e) => setForm({ ...form, password: e.target.value })} />
      </label>
      {tooShort && <small className="warn" role="alert">Use at least {MIN_LENGTH} characters.</small>}
      <label>Repeat the password
        <input type="password" autoComplete="new-password" value={form.confirm} required
          onChange={(e) => setForm({ ...form, confirm: e.target.value })} />
      </label>
      {mismatch && <small className="warn" role="alert">The passwords do not match.</small>}
      {state.error && <div className="state error" role="alert">{state.error}</div>}
      <button type="submit" disabled={state.busy || mismatch || tooShort}>{state.busy ? "Creating…" : "Create account"}</button>
      <p>Already registered? <Link to="/login">Sign in</Link></p>
    </form>
  );
}
