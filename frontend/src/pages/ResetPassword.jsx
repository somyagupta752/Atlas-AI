import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api } from "../api";
import { describeAuthError } from "./Login";

// Two steps on one page. 1) Ask for a reset: the server answers the same way whether or not the account exists.
// 2) Open the link from the message: the single-use token is in the URL FRAGMENT (#token=...), which browsers never send
// to a server or put in a Referer header; it is read once and removed from the address bar.
export default function ResetPassword() {
  const navigate = useNavigate();
  const location = useLocation();
  const [token, setToken] = useState(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [state, setState] = useState({ busy: false, error: null, done: false });

  // Follows the router's location, not just the first render: a reset link pasted into a tab that is already on this page
  // changes only the fragment, so nothing remounts. The fragment is read once and removed from the address bar.
  useEffect(() => {
    const match = /(?:^#|&)token=([^&]+)/.exec(location.hash);
    if (match) {
      setToken(decodeURIComponent(match[1]));
      navigate({ pathname: location.pathname, search: location.search }, { replace: true });
    }
  }, [location.hash, location.pathname, location.search, navigate]);

  async function request(event) {
    event.preventDefault();
    setState({ busy: true, error: null, done: false });
    try {
      await api.requestReset(email);
      setState({ busy: false, error: null, done: true });
    } catch (error) {
      setState({ busy: false, error: describeAuthError(error), done: false });
    }
  }

  async function confirm(event) {
    event.preventDefault();
    setState({ busy: true, error: null, done: false });
    try {
      await api.confirmReset(token, password);
      navigate("/login", { replace: true });
    } catch (error) {
      setState({ busy: false, error: describeAuthError(error), done: false });
    }
  }

  if (token) {
    return (
      <form onSubmit={confirm} className="form narrow" aria-label="Choose a new password">
        <h1>Choose a new password</h1>
        <label>New password (at least 12 characters)
          <input type="password" autoComplete="new-password" value={password} required minLength={12}
            onChange={(e) => setPassword(e.target.value)} />
        </label>
        {state.error && <div className="state error" role="alert">{state.error}</div>}
        <button type="submit" disabled={state.busy}>{state.busy ? "Saving…" : "Save the new password"}</button>
        <p><small>Saving ends every other session of your account.</small></p>
      </form>
    );
  }
  return (
    <form onSubmit={request} className="form narrow" aria-label="Reset your password">
      <h1>Reset your password</h1>
      <label>Email
        <input type="email" value={email} required onChange={(e) => setEmail(e.target.value)} />
      </label>
      {state.error && <div className="state error" role="alert">{state.error}</div>}
      {state.done && <div className="state" role="status">If the account exists, a reset message has been sent.</div>}
      <button type="submit" disabled={state.busy}>{state.busy ? "Sending…" : "Send reset message"}</button>
      <p><Link to="/login">Back to sign in</Link></p>
    </form>
  );
}
