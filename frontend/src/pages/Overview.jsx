import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useLoad } from "../hooks";
import { Async, RunLink, Status } from "../ui";
import { counts, money, pct } from "../format";

// Headline numbers are read from each completed run's stored metrics; nothing is precomputed here.
export default function Overview() {
  const runs = useLoad(() => api.runs({ limit: 50 }), [], {
    pollMs: 5000,
    shouldPoll: (d) => d.items.some((r) => r.status === "running" || r.status === "pending"),
  });
  return (
    <>
      <h1>Evaluations</h1>
      <Async state={runs} what="evaluations" isEmpty={(d) => d.items.length === 0}
        empty={<>No evaluations yet. <Link to="/start">Start one</Link> (a reference run is free).</>}>
        {(d) => <Content runs={d.items} total={d.page.total} />}
      </Async>
    </>
  );
}

function Content({ runs, total }) {
  const completed = runs.filter((r) => r.status === "completed");
  const [headline, setHeadline] = useState({ loading: true, best: null, costs: [] });
  useEffect(() => {
    let alive = true;
    Promise.all(completed.map((r) => api.metrics(r.id).then((m) => ({ run: r, m })).catch(() => null))).then((all) => {
      if (!alive) return;
      const rows = all.filter(Boolean).map(({ run, m }) => ({
        run, verified: m.metrics.find((x) => x.name === "final_verified_rate"),
        cost: m.metrics.find((x) => x.name === "cost_per_successful_repair"),
      }));
      const best = rows.filter((r) => r.verified && r.verified.value !== null)
        .sort((a, b) => b.verified.value - a.verified.value)[0] || null;
      setHeadline({ loading: false, best, costs: rows.filter((r) => r.cost && r.cost.value !== null).map((r) => r.cost.value) });
    });
    return () => { alive = false; };
  }, [completed.map((r) => r.id).join()]);

  const avgCost = headline.costs.length ? headline.costs.reduce((a, b) => a + b, 0) / headline.costs.length : null;
  return (
    <>
      <section className="cards">
        <div className="card"><span>Total evaluations</span><strong>{total}</strong></div>
        <div className="card"><span>Most recent</span><strong><RunLink run={runs[0]} /></strong><small><Status value={runs[0].status} /></small></div>
        <div className="card"><span>Best final verified rate</span>
          {headline.loading ? <strong>…</strong> : headline.best ? (
            <><strong>{pct(headline.best.verified.value)}</strong>
              <small>{counts(headline.best.verified)} · <RunLink run={headline.best.run} /></small></>
          ) : <strong>n/a</strong>}</div>
        <div className="card"><span>Avg cost per successful repair</span>
          <strong>{headline.loading ? "…" : money(avgCost)}</strong>
          <small>over {headline.costs.length} run(s) with a known cost</small></div>
      </section>
      <h2>Recent evaluations</h2>
      <div className="scroll">
        <table>
          <thead><tr><th>Name</th><th>Mode</th><th>Model</th><th>Status</th><th>Cases</th><th>Cost</th><th>Created</th></tr></thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td><RunLink run={r} />{r.is_historical && <span className="tag">historical</span>}</td>
                <td>{r.evaluation_mode}</td>
                <td>{(r.configuration_snapshot.model || {}).model_id || "unknown"}</td>
                <td><Status value={r.status} /></td>
                <td>{Object.values(r.case_status_counts).reduce((a, b) => a + b, 0)} / {r.total_cases}</td>
                <td>{money(r.total_cost_usd)}</td>
                <td>{new Date(r.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p><Link to="/compare">Compare evaluations →</Link></p>
    </>
  );
}
