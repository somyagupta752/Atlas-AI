import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useLoad } from "../hooks";
import { Async, Donut, HBars, PageHead, RunLink, Stat, Status } from "../ui";
import { counts, money, pct } from "../format";

// Headline numbers are read from each completed run's stored metrics; nothing is precomputed here.
export default function Overview() {
  const runs = useLoad(() => api.runs({ limit: 50 }), [], {
    pollMs: 5000,
    shouldPoll: (d) => d.items.some((r) => r.status === "running" || r.status === "pending"),
  });
  return (
    <>
      <PageHead eyebrow="Control plane" title="Evaluations">
        Every run of the repair pipeline, with the counts behind each number. Unknown values show as n/a, never as zero.
      </PageHead>
      <Async state={runs} what="evaluations" isEmpty={(d) => d.items.length === 0}
        empty={<>No evaluations yet. <Link to="/start">Start one</Link> (a reference run is free).</>}>
        {(d) => <Content runs={d.items} total={d.page.total} />}
      </Async>
    </>
  );
}

function Content({ runs, total }) {
  const completed = runs.filter((r) => r.status === "completed");
  const [headline, setHeadline] = useState({ loading: true, best: null, costs: [], rows: [] });
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
      setHeadline({ loading: false, best, rows: rows.filter((r) => r.verified && r.verified.value !== null), costs: rows.filter((r) => r.cost && r.cost.value !== null).map((r) => r.cost.value) });
    });
    return () => { alive = false; };
  }, [completed.map((r) => r.id).join()]);

  const avgCost = headline.costs.length ? headline.costs.reduce((a, b) => a + b, 0) / headline.costs.length : null;
  return (
    <>
      <section className="cards" style={{ marginTop: 0 }}>
        <Stat icon="list" label="Total evaluations">{total}</Stat>
        <Stat icon="play" label="Most recent" note={<Status value={runs[0].status} />}><RunLink run={runs[0]} /></Stat>
        <Stat icon="trophy" tone={headline.best ? "good" : ""} label="Best final verified rate"
          note={headline.best ? <>{counts(headline.best.verified)} · <RunLink run={headline.best.run} /></> : null}>
          {headline.loading ? "…" : headline.best ? pct(headline.best.verified.value) : "n/a"}</Stat>
        <Stat icon="dollar" label="Avg cost per successful repair" note={`over ${headline.costs.length} run(s) with a known cost`}>
          {headline.loading ? "…" : money(avgCost)}</Stat>
      </section>
      <div className="grid-2">
        <div className="panel">
          <h2>Final verified rate by run</h2>
          {headline.rows.length === 0 ? <small>{headline.loading ? "Loading…" : "No completed run has a verified rate yet."}</small> : (
            <HBars max={1} color="#5eead4" format={pct} items={headline.rows.slice(0, 8).map((r) => ({ label: r.run.name, value: r.verified.value }))} />
          )}
        </div>
        <div className="panel">
          <h2>Runs by status</h2>
          <Donut caption="runs" centre={runs.length}
            items={Object.entries(runs.reduce((m, r) => ({ ...m, [r.status]: (m[r.status] || 0) + 1 }), {})).map(([label, value]) => ({ label, value }))} />
        </div>
      </div>
      <section><h2>Recent evaluations</h2>
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
      </div></section>
      <p><Link to="/compare">Compare evaluations →</Link></p>
    </>
  );
}
