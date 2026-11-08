import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { useLoad } from "../hooks";
import { Async, Status } from "../ui";
import { counts, metricValue, money, seconds, tri } from "../format";

const ACTIVE = (r) => r.status === "pending" || r.status === "running";

export default function EvaluationDetail() {
  const { id } = useParams();
  const run = useLoad(() => api.run(id), [id], { pollMs: 3000, shouldPoll: ACTIVE });
  return (
    <Async state={run} what="evaluation">
      {(r) => <Detail run={r} reload={run.reload} />}
    </Async>
  );
}

function Detail({ run, reload }) {
  const active = ACTIVE(run);
  const metrics = useLoad(() => api.metrics(run.id), [run.id, run.status], { pollMs: 5000, shouldPoll: () => active });
  const cases = useLoad(() => api.cases(run.id), [run.id, run.status, JSON.stringify(run.case_status_counts)]);
  const model = (run.configuration_snapshot.model || {}).model_id || "unknown";
  const [note, setNote] = useState(null);
  const act = async (fn) => { try { setNote(await fn()); reload(); } catch (e) { setNote(`Error: ${e.message}`); } };
  return (
    <>
      <h1>{run.name} <Status value={run.status} /></h1>
      {active && <p className="state" role="status">Running… updating automatically. {Object.entries(run.case_status_counts).map(([k, v]) => `${v} ${k}`).join(", ")}</p>}
      <dl className="facts">
        <dt>Mode</dt><dd>{run.evaluation_mode} · diagnosis {run.diagnosis_mode}</dd>
        <dt>Configuration</dt><dd>{run.configuration_snapshot.name || "none (historical import)"} {run.configuration_snapshot.configuration_version ? `v${run.configuration_snapshot.configuration_version}` : ""} · {model} · retrieval {(run.configuration_snapshot.retrieval || {}).strategy || "unknown"} · tests {(run.configuration_snapshot.test_execution || {}).test_visibility || "unknown"}</dd>
        <dt>Benchmark</dt><dd>{run.benchmark_version} · {run.total_cases} case record(s)</dd>
        <dt>Cost</dt><dd>{money(run.total_cost_usd)} {run.cost_kind ? `(${run.cost_kind})` : ""}{run.budget_limit_usd ? ` of ${money(run.budget_limit_usd)} budget` : ""}</dd>
        <dt>Duration</dt><dd>{seconds(run.total_duration_seconds)}</dd>
        {run.environment.application && <><dt>Commit</dt><dd>{(run.environment.application.commit || "unknown").slice(0, 10)}{run.environment.application.dirty ? " (uncommitted changes)" : ""}</dd></>}
        {run.is_historical && <><dt>Source</dt><dd>historical import of {run.source_filename}</dd></>}
        {run.failure_reason && <><dt>Note</dt><dd>{run.failure_reason}</dd></>}
      </dl>
      {run.environment.determinism && <p><small>{run.environment.determinism}</small></p>}
      <div className="actions">
        {active && <button onClick={() => act(async () => { await api.cancel(run.id); return "Cancel requested"; })}>Cancel</button>}
        {run.status === "completed" && <button onClick={() => act(async () => { await api.createBaseline(run.id); return "Saved as a baseline"; })}>Use as baseline</button>}
        <Link to={`/compare?ids=${run.id}`}>Compare…</Link>
      </div>
      {note && <p className="state" role="status">{note}</p>}

      <h2>Metrics</h2>
      <Async state={metrics} what="metrics">
        {(m) => (
          <>
            <p><small>Population: {Object.entries(m.populations).map(([k, v]) => `${k.replaceAll("_", " ")} ${v}`).join(" · ")}. Calculation v{m.calculation_version ?? "—"}.</small></p>
            {m.metrics.length === 0 ? <p className="state empty">No metrics calculated yet.</p> : (
              <div className="scroll"><table>
                <thead><tr><th>Metric</th><th>Value</th><th>Numerator / denominator</th></tr></thead>
                <tbody>{m.metrics.map((x) => <tr key={x.name}><td>{x.name.replaceAll("_", " ")}</td><td>{metricValue(x)}</td><td>{counts(x)}</td></tr>)}</tbody>
              </table></div>
            )}
            {Object.keys(m.by_category).length > 0 && (
              <details><summary>By category</summary>
                {Object.entries(m.by_category).map(([cat, list]) => (
                  <p key={cat}><strong>{cat}</strong>: {["final_verified_rate", "heldout_pass_rate", "regression_rate"].map((n) => {
                    const x = list.find((y) => y.name === n); return x ? `${n.replaceAll("_", " ")} ${metricValue(x)} (${counts(x)})` : null;
                  }).filter(Boolean).join(" · ")}</p>
                ))}
              </details>
            )}
          </>
        )}
      </Async>

      <h2>Cases</h2>
      <Async state={cases} what="cases" isEmpty={(d) => d.items.length === 0} empty="No case has started yet.">
        {(d) => (
          <div className="scroll"><table>
            <thead><tr><th>Case</th><th>Variant</th><th>Status</th><th>Attempts</th><th>1st try</th><th>Final</th><th>Held-out</th><th>Regression</th><th>Cost</th><th>Latency</th></tr></thead>
            <tbody>{d.items.map((c) => (
              <tr key={c.id}>
                <td><Link to={`/runs/${run.id}/cases/${c.case_id}?variant=${encodeURIComponent(c.variant)}`}>{c.case_id}</Link><br /><small>{c.category || "uncategorised"}</small></td>
                <td>{c.variant}</td><td><Status value={c.status} />{c.failure_reason && <small> {c.failure_reason}</small>}</td>
                <td>{c.attempts_count}</td><td>{tri(c.first_attempt_verified)}</td><td>{tri(c.final_verified)}</td>
                <td>{tri(c.heldout_passed)}{c.exploratory_passed !== null && <small> · exploratory {tri(c.exploratory_passed)}</small>}</td>
                <td>{tri(c.regression_detected)}</td><td>{money(c.total_cost_usd)}</td><td>{seconds(c.total_latency_seconds)}</td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Async>
      <Baselines run={run} />
    </>
  );
}

function Baselines({ run }) {
  const baselines = useLoad(() => api.baselines(), []);
  const [result, setResult] = useState(null);
  if (!baselines.data || baselines.data.items.length === 0 || run.status === "pending" || run.status === "running") return null;
  const others = baselines.data.items.filter((b) => b.evaluation_run_id !== run.id);
  if (others.length === 0) return null;
  const check = async (b) => {
    try { setResult({ id: b.id, ...(await api.regressionCheck(run.id, b.id)) }); } catch (e) { setResult({ id: b.id, error: e.message }); }
  };
  return (
    <>
      <h2>Regression check against a baseline</h2>
      <ul>{others.map((b) => <li key={b.id}>{b.name} ({b.benchmark_version}) <button onClick={() => check(b)}>Check</button></li>)}</ul>
      {result && (result.error ? <div className="state error" role="alert">{result.error}</div> : (
        <div className={`state ${result.status === "regressed" ? "error" : ""}`}>
          <strong>{result.status === "regressed" ? "Regressions found" : "No threshold crossed"}</strong> over {result.compared_cases} shared case(s).
          <ul>{result.findings.map((f, i) => (
            <li key={i}>{f.message} {f.supporting_case_ids.length > 0 && <>Cases: {f.supporting_case_ids.map(([c]) => c).join(", ")}. </>}{f.warning && <em>{f.warning}</em>}</li>))}</ul>
        </div>))}
    </>
  );
}
