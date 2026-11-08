import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { can, whyNot } from "../auth/roles";
import { useLoad } from "../hooks";
import { Async, Empty } from "../ui";
import { friendly, RoleBadge } from "./Repositories";

// Every control is shown according to the caller's role, and an unavailable one says why. The server checks the role on
// every request regardless: this page cannot grant anything.
export default function RepositoryDetail() {
  const { id } = useParams();
  const detail = useLoad(() => api.repository(id), [id]);
  return (
    <>
      <p><Link to="/repos">← Repositories</Link></p>
      <Async state={detail} what="repository">
        {(repo) => (
          <>
            <h1>{repo.repository_id} <RoleBadge role={repo.role} /></h1>
            <details><summary>What your role allows</summary>
              <ul>{repo.capabilities.map((c) => <li key={c}>{c}</li>)}</ul>
            </details>
            <Index id={id} role={repo.role} />
            <Search id={id} role={repo.role} />
            {can(repo.role, "owner") && <Members id={id} />}
          </>
        )}
      </Async>
    </>
  );
}

function Index({ id, role }) {
  const status = useLoad(() => api.indexStatus(id), [id]);
  const [state, setState] = useState({ busy: false, error: null, done: false });
  const reason = whyNot(role, "editor");

  async function reindex() {
    const path = status.data && status.data.root_path;
    if (!path) return;
    setState({ busy: true, error: null, done: false });
    try {
      await api.indexRepository(id, path);
      setState({ busy: false, error: null, done: true });
      status.reload();
    } catch (error) {
      setState({ busy: false, error: friendly(error), done: false });
    }
  }

  return (
    <section>
      <h2>Index</h2>
      <Async state={status} what="index status">
        {(s) => (
          <dl className="facts">
            <dt>Status</dt><dd>{s.status} (version {s.index_version})</dd>
            <dt>Contents</dt><dd>{s.file_count} files, {s.chunk_count} chunks{s.failed_file_count ? `, ${s.failed_file_count} failed` : ""}</dd>
            <dt>Last indexed</dt><dd>{s.last_indexed_at ? new Date(s.last_indexed_at).toLocaleString() : "never"}</dd>
            {s.last_error && <><dt>Last error</dt><dd>{s.last_error}</dd></>}
          </dl>
        )}
      </Async>
      {status.error && !status.data && <Empty>{status.error.includes("No index") ? "This repository has not been indexed yet." : null}</Empty>}
      <button onClick={reindex} disabled={state.busy || !!reason || !(status.data && status.data.root_path)}
        title={reason || undefined}>{state.busy ? "Indexing…" : "Re-index"}</button>
      {reason && <small className="muted"> {reason}</small>}
      {state.error && <div className="state error" role="alert">{state.error}</div>}
      {state.done && <div className="state" role="status">Index updated.</div>}
    </section>
  );
}

function Search({ id, role }) {
  const [query, setQuery] = useState("");
  const [state, setState] = useState({ busy: false, error: null, results: null });
  const reason = whyNot(role, "viewer");

  async function submit(event) {
    event.preventDefault();
    setState({ busy: true, error: null, results: null });
    try {
      const response = await api.search(id, query);
      setState({ busy: false, error: null, results: response.results });
    } catch (error) {
      setState({ busy: false, results: null, error: friendly(error) });
    }
  }

  return (
    <section>
      <h2>Search code</h2>
      <form onSubmit={submit} className="inline-form" aria-label="Search code">
        <input value={query} required maxLength={4000} placeholder="Where is the user loaded?" aria-label="Search query"
          onChange={(e) => setQuery(e.target.value)} disabled={!!reason} />
        <button type="submit" disabled={state.busy || !!reason}>{state.busy ? "Searching…" : "Search"}</button>
      </form>
      {state.error && <div className="state error" role="alert">{state.error}</div>}
      {state.results && state.results.length === 0 && <Empty>No matching code.</Empty>}
      {state.results && state.results.length > 0 && (
        <ol className="results">{state.results.map((r) => (
          <li key={`${r.file_path}:${r.start_line}`}>
            <strong>{r.file_path}:{r.start_line}</strong> {r.qualified_name && <code>{r.qualified_name}</code>}
            <pre className="code">{r.content}</pre>
          </li>))}</ol>
      )}
    </section>
  );
}

function Members({ id }) {
  const members = useLoad(() => api.members(id), [id]);
  const [form, setForm] = useState({ email: "", role: "viewer" });
  const [state, setState] = useState({ busy: false, error: null });

  async function change(email, role) {
    setState({ busy: true, error: null });
    try {
      await api.setMember(id, email, role);
      setState({ busy: false, error: null });
      members.reload();
    } catch (error) {
      setState({ busy: false, error: friendly(error) });
    }
  }

  async function remove(userId) {
    setState({ busy: true, error: null });
    try {
      await api.removeMember(id, userId);
      setState({ busy: false, error: null });
      members.reload();
    } catch (error) {
      setState({ busy: false, error: friendly(error) });
    }
  }

  return (
    <section>
      <h2>Members</h2>
      <Async state={members} what="members">
        {(d) => (
          <div className="scroll"><table>
            <thead><tr><th>Email</th><th>Role</th><th /></tr></thead>
            <tbody>{d.members.map((m) => (
              <tr key={m.user_id}>
                <td>{m.email}</td>
                <td>
                  <select value={m.role} aria-label={`Role of ${m.email}`} disabled={state.busy}
                    onChange={(e) => change(m.email, e.target.value)}>
                    {["viewer", "editor", "owner"].map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                </td>
                <td><button className="secondary" disabled={state.busy} onClick={() => remove(m.user_id)}
                  aria-label={`Remove ${m.email}`}>Remove</button></td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Async>
      <form className="inline-form" aria-label="Add a member" onSubmit={(e) => { e.preventDefault(); change(form.email, form.role); }}>
        <input type="email" required placeholder="colleague@example.com" aria-label="Member email" value={form.email}
          onChange={(e) => setForm({ ...form, email: e.target.value })} />
        <select aria-label="New member role" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
          {["viewer", "editor", "owner"].map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <button type="submit" disabled={state.busy}>Add or update</button>
      </form>
      {state.error && <div className="state error" role="alert">{state.error}</div>}
    </section>
  );
}
