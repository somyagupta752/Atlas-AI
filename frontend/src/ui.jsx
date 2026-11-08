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

// Small stroke icon set (24x24, currentColor). Decorative: hidden from assistive tech.
const ICONS = {
  grid: "M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z",
  repo: "M4 4h12a4 4 0 0 1 4 4v12H8a4 4 0 0 1-4-4zM8 20v-4a4 4 0 0 0-4 0",
  play: "M6 4l14 8-14 8z",
  compare: "M8 3v18M16 3v18M3 8h5M16 16h5",
  bolt: "M13 2L4 14h7l-1 8 9-12h-7z",
  graph: "M6 6a2 2 0 1 0 0 .01M18 6a2 2 0 1 0 0 .01M12 18a2 2 0 1 0 0 .01M7.5 7.5l3.5 9M16.5 7.5L13 16.5M8 6h8",
  check: "M5 12l5 5 9-10",
  alert: "M12 3l10 18H2zM12 10v5M12 18v.01",
  wrench: "M14 6a4 4 0 0 0 5 5l-9 9a2 2 0 0 1-3-3l9-9a4 4 0 0 0-2-2z",
  dollar: "M12 2v20M17 6H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6",
  list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
  trophy: "M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0zM7 6H3v2a4 4 0 0 0 4 4M17 6h4v2a4 4 0 0 1-4 4",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-5-5",
  radio: "M5 12a7 7 0 0 1 14 0M8 12a4 4 0 0 1 8 0M12 12v.01",
};

export function Icon({ name, size = 18 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d={ICONS[name] || ""} /></svg>
  );
}

export function PageHead({ eyebrow, title, children, badge }) {
  return (
    <div className="page-head">
      {eyebrow && <div className="eyebrow">{eyebrow}</div>}
      <h1>{title} {badge}</h1>
      {children && <p className="lede">{children}</p>}
    </div>
  );
}

// A summary card: icon, small caption, big value, optional footnote. `tone` colours the value (good/warn/bad).
export function Stat({ icon, label, tone, children, note }) {
  return (
    <div className={`card ${tone || ""}`}>
      {icon && <div className="ico"><Icon name={icon} /></div>}
      <span>{label}</span>
      <strong>{children}</strong>
      {note && <small>{note}</small>}
    </div>
  );
}

export const PALETTE = ["#5eead4", "#fbbf24", "#7aa2ff", "#b69cff", "#f472b6", "#4ade80", "#fb923c", "#94a3b8"];

// Ring chart of [{label, value}] with the total in the middle.
export function Donut({ items, centre, caption }) {
  const total = items.reduce((a, b) => a + b.value, 0);
  const r = 62, c = 2 * Math.PI * r;
  let offset = 0;
  return (
    <div className="donut-wrap">
      <svg className="donut" viewBox="0 0 170 170" role="img" aria-label={`${caption}: ${items.map((i) => `${i.label} ${i.value}`).join(", ")}`}>
        <g transform="rotate(-90 85 85)">
          <circle cx="85" cy="85" r={r} fill="none" stroke="#111a26" strokeWidth="22" />
          {total > 0 && items.map((it, i) => {
            const len = (it.value / total) * c;
            const el = <circle key={it.label} cx="85" cy="85" r={r} fill="none" stroke={PALETTE[i % PALETTE.length]} strokeWidth="22"
              strokeDasharray={`${Math.max(len - 1.5, 0)} ${c}`} strokeDashoffset={-offset} />;
            offset += len;
            return el;
          })}
        </g>
        <text x="85" y="88" textAnchor="middle" fontSize="26">{centre}</text>
        <text x="85" y="108" textAnchor="middle" fontSize="9" style={{ fill: "#8b9bb0", letterSpacing: ".12em" }}>{caption.toUpperCase()}</text>
      </svg>
      <div className="legend-list">
        {items.map((it, i) => (
          <div key={it.label}><span className="swatch" style={{ background: PALETTE[i % PALETTE.length] }} />{it.label}<b>{it.value.toLocaleString()}</b></div>
        ))}
      </div>
    </div>
  );
}

// Horizontal bars; `max` defaults to the largest value.
export function HBars({ items, max, color, format = (v) => v.toLocaleString() }) {
  const top = max ?? Math.max(1, ...items.map((i) => i.value));
  return (
    <div className="hbars">
      {items.map((it, i) => (
        <div className="hbar" key={it.label}>
          <span title={it.label}>{it.label}</span>
          <span className="track"><span className="fill" style={{ display: "block", width: `${Math.min(100, (it.value / top) * 100)}%`, background: color || PALETTE[i % PALETTE.length] }} /></span>
          <b>{format(it.value)}</b>
        </div>
      ))}
    </div>
  );
}
