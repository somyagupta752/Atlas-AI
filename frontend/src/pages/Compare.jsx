import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useLoad } from "../hooks";
import { Async, ErrorBox } from "../ui";
import { bestIndex, counts, metricValue } from "../format";

// Which direction is "better" for the highlighted cell. Metrics without an entry are never highlighted.
const DIRECTION = {
  final_verified_rate: true, first_attempt_verified_rate: true, heldout_pass_rate: true, diagnosis_accuracy: true,
  patch_validity_rate: true, retry_success_rate: true, success_rate: true, regression_rate: false,
  average_latency_seconds: false, average_cost_per_case: false, total_cost_usd: false, cost_per_successful_repair: false,
};

export default function Compare() {
  const [params, setParams] = useSearchParams();
  const runs = useLoad(() => api.runs({ limit: 100 }), []);
  const selected = (params.get("ids") || "").split(",").filter(Boolean);
  const toggle = (id) => {
    const next = selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id];
    setParams(next.length ? { ids: next.join(",") } : {});
  };
  return (
    <>
      <h1>Compare evaluations</h1>
      <Async state={runs} what="evaluations" isEmpty={(d) => d.items.length < 2} empty="Two evaluations are needed to compare.">
        {(d) => (
          <fieldset><legend>Choose 2–10 evaluations</legend>
            {d.items.filter((r) => r.status !== "pending" && r.status !== "running").map((r) => (
              <label key={r.id} className="inline"><input type="checkbox" checked={selected.includes(r.id)} onChange={() => toggle(r.id)} /> {r.name} <small>({r.status}, {r.evaluation_mode})</small></label>
            ))}
          </fieldset>
        )}
      </Async>
      {selected.length >= 2 ? <Result ids={selected} /> : <p className="state empty">Select at least two evaluations.</p>}
    </>
  );
}

function Result({ ids }) {
  const result = useLoad(() => api.compare(ids), [ids.join()]);
  return (
    <Async state={result} what="comparison">
      {(c) => (
        <>
          {c.warnings.map((w, i) => <p key={i} className="state error" role="alert">{w}</p>)}
          <p className="state">{c.sample_note}</p>
          <div className="scroll"><table>
            <thead><tr><th>Metric</th>{c.runs.map((r) => <th key={r.id}><Link to={`/runs/${r.id}`}>{r.name}</Link><br /><small>{r.cases} cases</small></th>)}<th>Change vs first</th></tr></thead>
            <tbody>{Object.entries(c.metrics).map(([name, m]) => {
              const best = name in DIRECTION ? bestIndex(m.runs, DIRECTION[name]) : null;
              return (
                <tr key={name}>
                  <td>{name.replaceAll("_", " ")}</td>
                  {m.runs.map((cell, i) => (
                    <td key={i} className={best === i ? "best" : ""}>{metricValue({ name, value: cell.value })}<br /><small>{counts(cell)}</small></td>
                  ))}
                  <td>{m.differences_from_first.map((d, i) => (
                    <div key={i}>{d.absolute === null ? <small>{d.note}</small> : <>{d.absolute > 0 ? "+" : ""}{name.includes("rate") || name.includes("accuracy") ? `${(d.absolute * 100).toFixed(1)} pp` : d.absolute.toFixed(3)}{d.relative !== null && <small> ({(d.relative * 100).toFixed(0)}%)</small>} <small>{d.direction}</small></>}
                      {d.warning && <small className="warn"> ⚠ n={d.sample_sizes.join("/")}</small>}</div>))}</td>
                </tr>);
            })}</tbody>
          </table></div>
          <h2>By category</h2>
          {Object.entries(c.by_category).map(([cat, metrics]) => (
            <details key={cat}><summary>{cat}</summary><div className="scroll"><table>
              <thead><tr><th>Metric</th>{c.runs.map((r) => <th key={r.id}>{r.name}</th>)}</tr></thead>
              <tbody>{Object.entries(metrics).map(([name, m]) => (
                <tr key={name}><td>{name.replaceAll("_", " ")}</td>{m.runs.map((cell, i) => <td key={i}>{metricValue({ name, value: cell.value })} <small>{counts(cell)}</small></td>)}</tr>))}</tbody>
            </table></div></details>))}
          <h2>Case by case</h2>
          {c.cases.map((cc) => (
            <div key={cc.other_run_id} className="scroll"><table>
              <thead><tr><th colSpan={2}>Run {c.runs.findIndex((r) => r.id === cc.other_run_id) + 1} vs run 1 ({cc.compared} shared cases)</th></tr></thead>
              <tbody>{[["both_solved", "Both solved"], ["only_base_solved", "Only first solved"], ["only_other_solved", "Only this run solved"],
                ["neither_solved", "Neither solved"], ["unjudged", "Not judged in one run"], ["regression_in_other_only", "Regression only in this run"],
                ["regression_in_base_only", "Regression only in first"], ["retries_in_other_only", "Retries only in this run"],
                ["retries_in_base_only", "Retries only in first"], ["missing_in_other", "Missing from this run (not counted as failures)"],
                ["missing_in_base", "Missing from first run (not counted as failures)"]].map(([k, label]) => (
                <tr key={k}><td>{label}</td><td>{cc[k].length ? cc[k].map(([id, v]) => (v === "live" ? id : `${id} (${v})`)).join(", ") : "—"}</td></tr>))}</tbody>
            </table></div>))}
        </>
      )}
    </Async>
  );
}

export { ErrorBox };
