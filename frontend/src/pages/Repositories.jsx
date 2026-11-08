import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useLoad } from "../hooks";
import { Async } from "../ui";

const ID_PATTERN = "[a-z0-9][a-z0-9._\\-]{0,99}";

export function RoleBadge({ role }) {
  return <span className={`badge role-${role}`} title={`Your role: ${role}`}>{role}</span>;
}

export default function Repositories() {
  const repositories = useLoad(() => api.repositories(), []);
  return (
    <>
      <div className="page-head">
        <div className="eyebrow">Workspace</div>
        <h1>Repositories</h1>
        <p className="lede">Code you have indexed. Open one to explore its graph, search it or manage members.</p>
      </div>
      <Async state={repositories} what="repositories" isEmpty={(d) => d.items.length === 0}
        empty="You have no repositories yet. Index one below: you become its owner.">
        {(d) => (
          <div className="scroll"><table>
            <thead><tr><th>Repository</th><th>Your role</th></tr></thead>
            <tbody>{d.items.map((r) => (
              <tr key={r.repository_id}>
                <td><Link to={`/repos/${encodeURIComponent(r.repository_id)}`}>{r.repository_id}</Link></td>
                <td><RoleBadge role={r.role} /></td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Async>
      <IndexForm onDone={repositories.reload} />
    </>
  );
}

function IndexForm({ onDone }) {
  const [form, setForm] = useState({ id: "", path: "" });
  const [state, setState] = useState({ busy: false, error: null, result: null });

  async function submit(event) {
    event.preventDefault();
    setState({ busy: true, error: null, result: null });
    try {
      const result = await api.indexRepository(form.id, form.path);
      setState({ busy: false, error: null, result });
      onDone();
    } catch (error) {
      setState({ busy: false, result: null, error: friendly(error) });
    }
  }

  return (
    <form onSubmit={submit} className="form" aria-label="Index a repository">
      <h2>Index a repository</h2>
      <p><small>Indexing registers the repository. If the identifier is new you become its owner; if it exists you need the editor role.</small></p>
      <label>Identifier
        <input value={form.id} required pattern={ID_PATTERN} maxLength={100} placeholder="my-service"
          onChange={(e) => setForm({ ...form, id: e.target.value })} />
      </label>
      <label>Directory on the server (inside an allowed root)
        <input value={form.path} required placeholder="/srv/repositories/my-service"
          onChange={(e) => setForm({ ...form, path: e.target.value })} />
      </label>
      {state.error && <div className="state error" role="alert">{state.error}</div>}
      {state.result && <div className="state" role="status">Indexed {state.result.chunks_total} code chunks.</div>}
      <button type="submit" disabled={state.busy}>{state.busy ? "Indexing…" : "Index repository"}</button>
    </form>
  );
}

export function friendly(error) {
  if (error.status === 403) return `Not allowed: ${error.message}`;
  if (error.status === 404) return "That repository does not exist, or you do not have access to it.";
  if (error.status === 409) return `Conflict: ${error.message}`;
  if (error.status === 503) return "A required service (embeddings or the graph store) is not available.";
  return error.message;
}
