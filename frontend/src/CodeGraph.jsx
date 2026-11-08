import { useMemo, useRef, useState } from "react";
import { api } from "./api";
import { can } from "./auth/roles";
import { useLoad } from "./hooks";
import { blastCone, diffSeeds, fileGraph, HEIGHT, layout, shortName, WIDTH } from "./graph";
import { Async, Donut, HBars, Icon, Stat } from "./ui";

const COLORS = { base: "#5f7a96", hot: "#5eead4", selected: "#e6fffb", cone: "#fb923c", seed: "#f87171" };
const SAMPLE_DIFF = "--- a/path/to/file.py\n+++ b/path/to/file.py\n@@ -1 +1,2 @@\n original line\n+new line";

// The code knowledge graph for one repository: stats, the import graph, hot files and a "blast a diff" tool.
// The graph is read from the API as stored; nothing here is estimated.
export default function CodeGraph({ id, role }) {
  const index = useLoad(() => api.indexStatus(id), [id]);
  const rootPath = index.data ? index.data.root_path : null;
  // A 404 means "no graph built yet", which is a normal state, not an error.
  const graph = useLoad(() => api.graph(id).catch((e) => { if (e.status === 404) return { missing: true }; if (e.status === 503) return { unavailable: e.message }; throw e; }), [id]);
  const stats = useLoad(() => api.graphStats(id).catch(() => null), [id]);
  const missing = graph.data && graph.data.missing;
  return (
    <section>
      <div className="panel-head"><h2>Code graph</h2></div>
      {graph.data && graph.data.unavailable ? (
        <p className="state empty">The graph store is not available here ({graph.data.unavailable}). The rest of the page works without it.</p>
      ) : missing ? <Build id={id} role={role} rootPath={rootPath} onDone={() => { graph.reload(); stats.reload(); }} /> : (
        <Async state={graph} what="code graph">
          {(g) => <Explorer graph={g} stats={stats.data} />}
        </Async>
      )}
    </section>
  );
}

function Build({ id, role, rootPath, onDone }) {
  const [state, setState] = useState({ busy: false, error: null });
  const allowed = can(role, "editor");
  async function build() {
    setState({ busy: true, error: null });
    try {
      await api.buildGraph(id, rootPath);
      setState({ busy: false, error: null });
      onDone();
    } catch (error) {
      setState({ busy: false, error: error.status === 503 ? "The graph store (Neo4j) is not available." : error.message });
    }
  }
  return (
    <div className="state empty">
      <p>No code graph has been built for this repository yet. Building it maps every file, import, class and call.</p>
      <button onClick={build} disabled={state.busy || !allowed || !rootPath}
        title={!allowed ? "The editor role is needed to build the graph" : !rootPath ? "Index the repository first" : undefined}>
        {state.busy ? "Building…" : "Build graph"}
      </button>
      {!allowed && <small className="muted"> The editor role is needed to build the graph.</small>}
      {!rootPath && allowed && <small className="muted"> Index the repository first.</small>}
      {state.error && <div className="state error" role="alert">{state.error}</div>}
    </div>
  );
}

function Explorer({ graph, stats }) {
  const model = useMemo(() => fileGraph(graph), [graph]);
  const view = useMemo(() => layout(model.files, model.edges, model.importers, model.imports), [model]);
  const [selected, setSelected] = useState(null);
  const [diff, setDiff] = useState("");
  const [blast, setBlast] = useState(null);

  const hot = useMemo(
    () => [...model.files.values()].map((f) => ({ ...f, n: model.importers.get(f.id).size })).filter((f) => f.n > 0)
      .sort((a, b) => b.n - a.n || (a.path < b.path ? -1 : 1)).slice(0, 10),
    [model],
  );
  const hotIds = useMemo(() => new Set(hot.slice(0, 8).map((f) => f.id)), [hot]);
  const neighbours = useMemo(() => {
    if (!selected) return new Set();
    return new Set([...model.importers.get(selected), ...model.imports.get(selected)]);
  }, [selected, model]);

  function runBlast(text) {
    const { seeds, missing } = diffSeeds(text, model.files);
    setBlast({ seeds, missing, cone: blastCone(seeds, model.importers) });
    setSelected(null);
  }

  const kinds = Object.entries(stats?.nodes_by_label ?? {}).map(([label, value]) => ({ label, value }));
  const rels = Object.entries(stats?.relationships_by_type ?? {}).map(([label, value]) => ({ label, value }));
  const sel = selected ? model.files.get(selected) : null;

  return (
    <>
      <div className="cards">
        <Stat icon="list" label="Files">{model.files.size.toLocaleString()}</Stat>
        <Stat icon="graph" label="Import edges">{model.edges.length.toLocaleString()}</Stat>
        <Stat icon="bolt" label="Definitions">{(graph.nodes.length - model.files.size - 1).toLocaleString()}</Stat>
        <Stat icon="alert" label="Unresolved references" note="honest gaps, never guessed">{graph.unresolved_references.length.toLocaleString()}</Stat>
      </div>
      <div className="grid-3" style={{ marginTop: 14 }}>
        <div style={{ display: "grid", gap: 14 }}>
          <div className="panel">
            <h2>Hot files</h2>
            <p><small>Most importers: the load-bearing files.</small></p>
            {hot.length === 0 ? <small>No file is imported by another.</small> : (
              <ul className="file-list">
                {hot.map((f) => (
                  <li key={f.id}>
                    <button className={selected === f.id ? "active" : ""} onClick={() => { setBlast(null); setSelected(f.id); }}>
                      <span className="name" title={f.path}>{shortName(f.path)}</span><span className="n">{f.n}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          {kinds.length > 0 && (
            <div className="panel"><h2>Node kinds</h2>
              <Donut items={kinds} centre={(stats?.total_nodes ?? 0).toLocaleString()} caption="nodes" />
            </div>
          )}
        </div>

        <GraphCanvas view={view} model={model} selected={selected} neighbours={neighbours} hotIds={hotIds} blast={blast}
          onSelect={(id) => { setBlast(null); setSelected(id); }} />

        <div style={{ display: "grid", gap: 14 }}>
          <div className="panel">
            <h2>{sel ? "Selected file" : "Nothing selected"}</h2>
            {sel ? <Detail file={sel} model={model} onPick={setSelected} /> : (
              <small>Click a node or a hot file to see what imports it and what it imports. Or paste a diff to see the blast radius.</small>
            )}
          </div>
          <div className="panel">
            <h2>Blast a diff</h2>
            <p><small>Paste a diff. Every file that depends on a changed file is highlighted: the upstream cone a regression could reach. Get one with <code>git diff</code>.</small></p>
            <textarea rows={6} value={diff} placeholder={SAMPLE_DIFF} aria-label="Diff to analyse" onChange={(e) => setDiff(e.target.value)} />
            <div className="actions">
              <button onClick={() => runBlast(diff)} disabled={!diff.trim()}>Show blast radius</button>
              {blast && <button className="secondary" onClick={() => { setBlast(null); setDiff(""); }}>Clear</button>}
            </div>
            {blast && (
              <div role="status">
                <div className="stat-row"><span>Files changed</span><b>{blast.seeds.size}</b></div>
                <div className="stat-row"><span>Files in the blast cone</span><b>{blast.cone.size}</b></div>
                {blast.missing.length > 0 && <p className="warn"><small>Not in the graph: {blast.missing.join(", ")}</small></p>}
                {blast.seeds.size === 0 && blast.missing.length === 0 && <small>No file names found in that text.</small>}
              </div>
            )}
          </div>
          {rels.length > 0 && <div className="panel"><h2>Relationships</h2><HBars items={rels} /></div>}
        </div>
      </div>
    </>
  );
}

function Detail({ file, model, onPick }) {
  const list = (ids) => [...ids].map((i) => model.files.get(i)).sort((a, b) => (a.path < b.path ? -1 : 1));
  const importers = list(model.importers.get(file.id)), imports = list(model.imports.get(file.id));
  const Links = ({ items }) => (
    <ul className="side-list">{items.slice(0, 12).map((f) => (
      <li key={f.id}><a href="#pick" onClick={(e) => { e.preventDefault(); onPick(f.id); }}>{f.path}</a></li>))}
      {items.length > 12 && <li>… and {items.length - 12} more</li>}
      {items.length === 0 && <li>none</li>}</ul>
  );
  return (
    <>
      <div style={{ fontFamily: "var(--mono)", overflowWrap: "anywhere" }}>{file.path}</div>
      <div className="stat-row"><span>Imported by</span><b>{importers.length}</b></div>
      <div className="stat-row"><span>Imports</span><b>{imports.length}</b></div>
      <div className="stat-row"><span>Definitions</span><b>{file.defs.length}</b></div>
      <h2 style={{ marginTop: 14 }}>Imported by</h2><Links items={importers} />
      <h2>Imports</h2><Links items={imports} />
      {file.defs.length > 0 && (<><h2>Defines</h2>
        <ul className="side-list">{file.defs.slice(0, 12).map((d, i) => <li key={i}>{d.kind} {d.name}{d.line ? `:${d.line}` : ""}</li>)}
          {file.defs.length > 12 && <li>… and {file.defs.length - 12} more</li>}</ul></>)}
    </>
  );
}

function GraphCanvas({ view, model, selected, neighbours, hotIds, blast, onSelect }) {
  const [cam, setCam] = useState({ x: 0, y: 0, k: 1 });
  const drag = useRef(null);
  const [dragging, setDragging] = useState(false);
  const pos = useMemo(() => new Map(view.nodes.map((n) => [n.id, n])), [view]);
  const zoom = (f) => setCam((c) => {
    const k = Math.min(4, Math.max(0.5, c.k * f));
    // keep the centre of the canvas fixed while zooming
    return { k, x: c.x + (WIDTH / 2 - c.x) * (1 - k / c.k), y: c.y + (HEIGHT / 2 - c.y) * (1 - k / c.k) };
  });
  const colour = (n) => (blast ? (blast.seeds.has(n.id) ? COLORS.seed : blast.cone.has(n.id) ? COLORS.cone : COLORS.base)
    : n.id === selected ? COLORS.selected : hotIds.has(n.id) ? COLORS.hot : COLORS.base);
  const labelled = (n) => n.id === selected || neighbours.has(n.id) || hotIds.has(n.id) || (blast && (blast.seeds.has(n.id) || (blast.cone.size <= 25 && blast.cone.has(n.id))));

  const onDown = (e) => { if (e.target.closest(".node")) return; drag.current = { x: e.clientX, y: e.clientY, cam }; setDragging(true); e.currentTarget.setPointerCapture?.(e.pointerId); };
  const onMove = (e) => {
    if (!drag.current) return;
    const scale = WIDTH / e.currentTarget.getBoundingClientRect().width;
    setCam({ ...drag.current.cam, x: drag.current.cam.x + (e.clientX - drag.current.x) * scale, y: drag.current.cam.y + (e.clientY - drag.current.y) * scale });
  };
  const onUp = () => { drag.current = null; setDragging(false); };

  return (
    <div className="graph-box">
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="group" aria-label={`Import graph, ${view.shown} of ${view.total} files shown`}
        className={dragging ? "dragging" : ""} onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp}>
        <g transform={`translate(${cam.x} ${cam.y}) scale(${cam.k})`}>
          {view.links.map((l) => {
            const a = pos.get(l.source), b = pos.get(l.target);
            const on = selected && (l.source === selected || l.target === selected);
            const cone = blast && (blast.cone.has(l.source) || blast.seeds.has(l.source)) && (blast.cone.has(l.target) || blast.seeds.has(l.target));
            return <line key={`${l.source}>${l.target}`} className={`edge ${on ? "on" : ""} ${cone ? "cone" : ""}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y} />;
          })}
          {view.nodes.map((n) => {
            const r = 4 + Math.min(10, Math.sqrt(n.indegree) * 3) + (n.id === selected ? 3 : 0);
            return (
              <g key={n.id} className="node" transform={`translate(${n.x} ${n.y})`} onClick={() => onSelect(n.id)}
                role="button" tabIndex={0} aria-label={model.files.get(n.id).path}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(n.id); } }}>
                <title>{model.files.get(n.id).path} · imported by {n.indegree}</title>
                <circle r={r} fill={colour(n)} />
                {labelled(n) && <text x={r + 4} y={3}>{shortName(model.files.get(n.id).path)}</text>}
              </g>
            );
          })}
        </g>
      </svg>
      <div className="graph-tools">
        <button className="secondary" onClick={() => zoom(1.25)} aria-label="Zoom in">+</button>
        <button className="secondary" onClick={() => zoom(0.8)} aria-label="Zoom out">−</button>
        <button className="secondary" onClick={() => setCam({ x: 0, y: 0, k: 1 })} aria-label="Reset view"><Icon name="search" size={13} /></button>
      </div>
      <div className="graph-legend" aria-hidden="true">
        <div><span className="dot" style={{ background: COLORS.hot }} />hot file (many importers)</div>
        <div><span className="dot" style={{ background: COLORS.selected }} />selected</div>
        <div><span className="dot" style={{ background: COLORS.cone }} />blast cone</div>
        <div><span className="dot" style={{ background: COLORS.seed }} />changed by the diff</div>
        <div>{view.shown} of {view.total} files shown{view.total > view.shown ? " (connected files only, best first)" : ""}</div>
      </div>
    </div>
  );
}
