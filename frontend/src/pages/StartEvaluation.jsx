import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { useLoad } from "../hooks";
import { Async } from "../ui";
import { money } from "../format";

// Opening this page never starts anything. A run starts only from the final confirmation button.
export default function StartEvaluation() {
  const configs = useLoad(() => api.configurations(), []);
  const bench = useLoad(() => api.benchmarks(), []);
  return (
    <>
      <h1>Start an evaluation</h1>
      <Async state={configs} what="configurations" isEmpty={(d) => d.items.length === 0}
        empty="No configuration exists yet. Create one with POST /evaluations/configurations.">
        {(c) => (
          <Async state={bench} what="benchmark cases">
            {(b) => <Form configs={c.items} bench={b} />}
          </Async>
        )}
      </Async>
    </>
  );
}

function Form({ configs, bench }) {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [configId, setConfigId] = useState(configs[0].id);
  const [mode, setMode] = useState("reference");
  const [diagnosis, setDiagnosis] = useState("recorded");
  const [selected, setSelected] = useState(new Set());
  const [budget, setBudget] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [state, setState] = useState({ busy: false, error: null });
  const config = configs.find((c) => c.id === configId);
  const live = mode === "live";
  const ids = [...selected];
  const units = bench.cases.filter((c) => selected.has(c.case_id))
    .reduce((n, c) => n + (live ? 1 : Math.max(c.reference_variants.length, 1)), 0);
  const maxBudget = config.config.budget.max_cost_usd;
  const budgetNumber = Number(budget);
  const budgetOk = !live || (budget !== "" && budgetNumber > 0 && budgetNumber <= maxBudget);
  const ready = name.trim() && (live ? ids.length > 0 : true) && budgetOk && (!live || confirm);

  const toggle = (id) => setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const toggleGroup = (cases) => setSelected((s) => {
    const n = new Set(s); const all = cases.every((c) => n.has(c)); cases.forEach((c) => (all ? n.delete(c) : n.add(c))); return n;
  });

  async function submit(e) {
    e.preventDefault();
    setState({ busy: true, error: null });
    try {
      const run = await api.start({
        name: name.trim(), configuration_id: configId, mode, diagnosis_mode: live ? diagnosis : "recorded",
        benchmark_ids: ids.length ? ids : null, max_cost_usd: live ? budgetNumber : null, confirm_paid: live && confirm,
      });
      navigate(`/runs/${run.id}`);
    } catch (err) {
      setState({ busy: false, error: err.message });
    }
  }

  return (
    <form onSubmit={submit} className="form">
      <label>Name<input value={name} onChange={(e) => setName(e.target.value)} maxLength={200} required /></label>
      <label>Configuration
        <select value={configId} onChange={(e) => setConfigId(e.target.value)}>
          {configs.map((c) => <option key={c.id} value={c.id}>{c.name} v{c.version} · {c.model_id} · {c.retrieval_strategy}</option>)}
        </select>
      </label>
      <fieldset>
        <legend>Mode</legend>
        <label className="inline"><input type="radio" checked={!live} onChange={() => setMode("reference")} /> Reference — fixed patches, no model calls, free</label>
        <label className="inline"><input type="radio" checked={live} onChange={() => setMode("live")} /> Live — real model calls, costs money</label>
      </fieldset>
      {live && (
        <>
          <label>Diagnosis
            <select value={diagnosis} onChange={(e) => setDiagnosis(e.target.value)}>
              <option value="recorded">Recorded diagnoses (patching only)</option>
              <option value="agent">Run the debugging agent first (more calls)</option>
            </select>
          </label>
          <label>Maximum budget (USD, hard cap for all model calls; at most {money(maxBudget)} for this configuration)
            <input type="number" min="0.01" max={maxBudget} step="0.01" value={budget} onChange={(e) => setBudget(e.target.value)} required />
          </label>
        </>
      )}
      <fieldset>
        <legend>Benchmark cases ({bench.version})</legend>
        {Object.entries(bench.categories).map(([category, cases]) => (
          <div key={category} className="group">
            <label className="inline"><input type="checkbox" checked={cases.every((c) => selected.has(c))} onChange={() => toggleGroup(cases)} /> <strong>{category}</strong></label>
            <div className="cases">
              {cases.map((c) => <label key={c} className="inline"><input type="checkbox" checked={selected.has(c)} onChange={() => toggle(c)} /> {c}</label>)}
            </div>
          </div>
        ))}
        {!live && <small>No case selected runs every case.</small>}
      </fieldset>
      <section className="summary" aria-live="polite">
        <h2>Review</h2>
        <ul>
          <li>{ids.length || (live ? 0 : bench.cases.length)} case(s) → {live ? units : units || bench.cases.reduce((n, c) => n + Math.max(c.reference_variants.length, 1), 0)} case record(s)</li>
          <li>Model: {config.model_id} · retrieval: {config.retrieval_strategy} · tests: {config.test_backend}, {config.test_visibility}</li>
          <li>{live ? `Paid model calls, capped at ${budget ? money(budgetNumber) : "(set a budget)"}. Cases the cap cannot cover are recorded as incomplete.` : "No model calls and no cost."}</li>
        </ul>
        {live && <label className="inline"><input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} /> I understand this makes paid model calls.</label>}
      </section>
      {state.error && <div className="state error" role="alert">{state.error}</div>}
      <button type="submit" disabled={!ready || state.busy}>{state.busy ? "Starting…" : live ? "Start paid evaluation" : "Start reference evaluation"}</button>
    </form>
  );
}
