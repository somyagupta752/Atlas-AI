import { Link } from "react-router-dom";

export function Loading({ what = "data" }) {
  return <p className="state" role="status">Loading {what}…</p>;
}

export function ErrorBox({ error, onRetry }) {
  return (
    <div className="state error" role="alert">
      <strong>Could not load.</strong> {error} {onRetry && <button onClick={onRetry}>Retry</button>}
    </div>
  );
}

export function Empty({ children }) {
  return <p className="state empty">{children}</p>;
}

// Loading, error and empty states in one place, so no page renders a blank or a fake number.
export function Async({ state, what, children, isEmpty, empty }) {
  if (state.loading && !state.data) return <Loading what={what} />;
  if (state.error && !state.data) return <ErrorBox error={state.error} onRetry={state.reload} />;
  if (state.data && isEmpty && isEmpty(state.data)) return <Empty>{empty}</Empty>;
  return state.data ? children(state.data) : null;
}

export function Status({ value }) {
  return <span className={`badge ${value}`}>{value}</span>;
}

export function RunLink({ run }) {
  return <Link to={`/runs/${run.id}`}>{run.name}</Link>;
}
