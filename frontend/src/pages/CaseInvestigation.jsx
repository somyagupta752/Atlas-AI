import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useLoad } from "../hooks";
import { Async, Status } from "../ui";
import { money, seconds, tri } from "../format";

export default function CaseInvestigation() {
  const { id, caseId } = useParams();
  const [params] = useSearchParams();
  const variant = params.get("variant") || undefined;
  const detail = useLoad(() => api.caseDetail(id, caseId, variant), [id, caseId, variant]);
  return (
    <Async state={detail} what="case">
      {(c) => <Case runId={id} c={c} />}
    </Async>
  );
}

function Json({ value }) {
  return <pre className="code">{JSON.stringify(value, null, 2)}</pre>;
}

function Case({ runId, c }) {
  const diagnosis = (c.evidence.diagnosis || {});
  const analysis = diagnosis.analysis;
  const execution = c.evidence.execution || {};
  const final = c.attempts[c.attempts.length - 1];
  return (
    <>
      <p><Link to={`/runs/${runId}`}>← Evaluation</Link></p>
      <h1>{c.case_id} <small>{c.variant}</small> <Status value={c.status} /></h1>
      {c.failure_reason && <p className="state error">{c.failure_reason}</p>}
      {c.other_variants.length > 0 && <p>Other variants: {c.other_variants.map((v) => <Link key={v} className="tag" to={`/runs/${runId}/cases/${c.case_id}?variant=${v}`}>{v}</Link>)}</p>}

      <h2>Why Atlas judged it this way</h2>
      <ul className="verdict">
        <li>Patch generated: {tri(c.patch_generated)} · valid: {tri(c.patch_valid)} · applied: {tri(c.patch_applied)}</li>
        <li>Verified on first attempt: {tri(c.first_attempt_verified)} · finally verified: {tri(c.final_verified)} · repaired by retry: {tri(c.repaired_by_retry)}</li>
        <li>Held-out tests: {tri(c.heldout_passed)} · exploratory (informational): {tri(c.exploratory_passed)} · regression detected: {tri(c.regression_detected)}</li>
        <li>Success (verified, expected files only, held-out not failing): {tri(c.success)}</li>
        <li>Cost {money(c.total_cost_usd)}{c.cost_kind ? ` (${c.cost_kind})` : ""} · latency {seconds(c.total_latency_seconds)}</li>
      </ul>

      <h2>Diagnosis</h2>
      {!analysis ? <p className="state empty">{diagnosis.source === "recorded" ? "A recorded diagnosis from the benchmark was used; no agent investigation ran." : "No diagnosis was stored."}</p> : (
        <>
          <p><strong>{analysis.root_cause || analysis.summary}</strong> (confidence {analysis.confidence ?? "n/a"}, correct: {tri(c.diagnosis_correct)})</p>
          {c.diagnosis_record_id && <p><small>Investigation record {c.diagnosis_record_id}</small></p>}
          <h3>Evidence collected</h3>
          {(analysis.evidence || []).length === 0 ? <p className="state empty">No evidence items.</p> : (
            <ul>{analysis.evidence.map((e) => <li key={e.id}><code>{e.id}</code> {e.source_type} — {e.file_path}{e.start_line ? `:${e.start_line}-${e.end_line}` : ""} {e.qualified_name && <code>{e.qualified_name}</code>}<br />{e.observation}</li>)}</ul>
          )}
          <details><summary>Retrieved code references and tools</summary><Json value={{ retrieval_sources: diagnosis.retrieval_sources, tools: diagnosis.tools, affected_files: analysis.affected_files }} /></details>
        </>
      )}

      <h2>Patch attempts</h2>
      {c.attempts.length === 0 ? <p className="state empty">No patch was attempted for this case.</p> : c.attempts.map((a) => (
        <article key={a.id} className="attempt">
          <h3>Attempt {a.attempt_number} <Status value={a.status} /></h3>
          <p>valid {tri(a.patch_valid)} · applied {tri(a.patch_applied)} · required tests passed {tri(a.required_tests_passed)} · regression {tri(a.regression_detected)}{a.heldout_tests_passed !== null && <> · held-out {tri(a.heldout_tests_passed)}</>} · {money(a.cost_usd)} · {seconds(a.latency_seconds)}</p>
          {a.feedback_from_attempts.length > 0 && (
            <div className="feedback"><strong>Received failure feedback from attempt {a.feedback_from_attempts.join(", ")}:</strong>
              <ul>{a.feedback_reasons.map((r, i) => <li key={i}><pre className="code">{r}</pre></li>)}</ul></div>
          )}
          {a.failure_reason && <p className="state error">{a.failure_reason}</p>}
          {a.evidence.rationale && <p><em>{a.evidence.rationale}</em></p>}
          {a.unified_diff && <details open={a === final}><summary>Generated patch</summary><pre className="code diff">{a.unified_diff}</pre></details>}
          {a.evidence.verification && (
            <details><summary>Verification checks and test results</summary>
              <ul>{a.evidence.verification.checks.map((k) => <li key={k.name}>{k.name}: <strong>{k.status}</strong>{k.reason ? ` — ${k.reason}` : ""}</li>)}</ul>
              {a.evidence.required_test_outcomes && <Json value={a.evidence.required_test_outcomes} />}
              <p>{a.evidence.verification.summary}</p>
            </details>)}
        </article>
      ))}

      <h2>Held-out and exploratory results</h2>
      {c.evidence.holdout ? <Json value={c.evidence.holdout} /> : <p className="state empty">No held-out tests apply, or none ran.</p>}
      {c.evidence.exploratory && <><h3>Exploratory (never scored)</h3><Json value={c.evidence.exploratory} /></>}
      {execution.source_snapshot && <details><summary>Execution record (baseline, test selection, budget)</summary><Json value={{ stop_reason: execution.stop_reason, baseline: execution.baseline, test_selection: execution.test_selection, llm_budget: execution.llm_budget }} /></details>}
    </>
  );
}
