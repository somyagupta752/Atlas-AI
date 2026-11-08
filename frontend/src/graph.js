// Pure helpers for the code graph view. Nothing here touches the DOM or the network.

export const MAX_NODES = 300;
export const WIDTH = 1000;
export const HEIGHT = 640;

// Files and the IMPORTS edges between them, plus what each file defines.
export function fileGraph(graph) {
  const files = new Map();
  for (const n of graph.nodes) {
    if (n.label === "File") files.set(n.id, { id: n.id, path: String(n.properties.path ?? n.id), defs: [] });
  }
  const owner = new Map();
  for (const r of graph.relationships) {
    if (r.type === "CONTAINS" && files.has(r.source_id)) owner.set(r.target_id, r.source_id);
  }
  for (const n of graph.nodes) {
    if ((n.label === "Class" || n.label === "Function") && owner.has(n.id)) {
      files.get(owner.get(n.id)).defs.push({ kind: n.label.toLowerCase(), name: String(n.properties.qualified_name ?? n.properties.name ?? n.id), line: n.properties.line_start ?? null });
    }
  }
  const seen = new Set();
  const edges = [];
  const importers = new Map([...files.keys()].map((k) => [k, new Set()]));
  const imports = new Map([...files.keys()].map((k) => [k, new Set()]));
  for (const r of graph.relationships) {
    if (r.type !== "IMPORTS" || !files.has(r.source_id) || !files.has(r.target_id) || r.source_id === r.target_id) continue;
    const key = `${r.source_id}>${r.target_id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    edges.push({ source: r.source_id, target: r.target_id });
    importers.get(r.target_id).add(r.source_id);
    imports.get(r.source_id).add(r.target_id);
  }
  return { files, edges, importers, imports };
}

// Files that depend on `seeds`, directly or through a chain of imports: what a regression could reach.
export function blastCone(seeds, importers) {
  const cone = new Set();
  const queue = [...seeds];
  while (queue.length) {
    const id = queue.pop();
    for (const up of importers.get(id) || []) {
      if (!cone.has(up) && !seeds.has(up)) {
        cone.add(up);
        queue.push(up);
      }
    }
  }
  return cone;
}

// File paths named in a unified diff. Matches a graph path exactly, or as a path suffix (diffs are often rooted elsewhere).
export function diffSeeds(diff, files) {
  const named = new Set();
  for (const line of diff.split("\n")) {
    const m = /^(?:\+\+\+|---) (?:[ab]\/)?(\S+)/.exec(line) || /^diff --git a\/(\S+) b\//.exec(line);
    if (m && m[1] !== "/dev/null") named.add(m[1]);
  }
  const seeds = new Set();
  const missing = [];
  for (const name of named) {
    const hit = [...files.values()].find((f) => f.path === name || f.path.endsWith(`/${name}`) || name.endsWith(`/${f.path}`));
    if (hit) seeds.add(hit.id);
    else missing.push(name);
  }
  return { seeds, missing };
}

// Small deterministic PRNG so the same graph always lays out the same way.
function prng(seed) {
  let s = seed >>> 0 || 1;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

// Keep the best-connected files when there are too many to draw, then run a force simulation (Fruchterman-Reingold).
export function layout(files, edges, importers, imports, { max = MAX_NODES, iterations = 220 } = {}) {
  const degree = (id) => importers.get(id).size + imports.get(id).size;
  // Files with no import edge at all would only drift to the border; they are counted in `total`, not drawn.
  const ids = [...files.keys()].filter((id) => degree(id) > 0).sort((a, b) => degree(b) - degree(a) || (a < b ? -1 : 1)).slice(0, max);
  const index = new Map(ids.map((id, i) => [id, i]));
  const links = edges.filter((e) => index.has(e.source) && index.has(e.target));
  const n = ids.length;
  const rand = prng(n * 7919 + links.length);
  const x = new Float64Array(n), y = new Float64Array(n);
  ids.forEach((_, i) => {
    const a = (i / Math.max(n, 1)) * Math.PI * 2, r = 80 + rand() * 220;
    x[i] = WIDTH / 2 + Math.cos(a) * r;
    y[i] = HEIGHT / 2 + Math.sin(a) * r * 0.7;
  });
  const k = Math.sqrt((WIDTH * HEIGHT) / Math.max(n, 1)) * 0.75;
  const dx = new Float64Array(n), dy = new Float64Array(n);
  for (let it = 0; it < iterations; it++) {
    const temp = 40 * (1 - it / iterations) + 1;
    dx.fill(0); dy.fill(0);
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        let ex = x[i] - x[j], ey = y[i] - y[j];
        let d2 = ex * ex + ey * ey;
        if (d2 < 0.01) { ex = rand() - 0.5; ey = rand() - 0.5; d2 = ex * ex + ey * ey + 0.01; }
        const f = (k * k) / d2;
        dx[i] += ex * f; dy[i] += ey * f; dx[j] -= ex * f; dy[j] -= ey * f;
      }
    }
    for (const l of links) {
      const i = index.get(l.source), j = index.get(l.target);
      const ex = x[i] - x[j], ey = y[i] - y[j];
      const d = Math.sqrt(ex * ex + ey * ey) || 0.01;
      const f = d / k;
      dx[i] -= ex * f; dy[i] -= ey * f; dx[j] += ex * f; dy[j] += ey * f;
    }
    for (let i = 0; i < n; i++) {
      dx[i] += (WIDTH / 2 - x[i]) * 0.02; dy[i] += (HEIGHT / 2 - y[i]) * 0.02; // gentle gravity keeps islands on screen
      const d = Math.sqrt(dx[i] * dx[i] + dy[i] * dy[i]) || 1;
      const m = Math.min(d, temp);
      x[i] = Math.min(WIDTH - 30, Math.max(30, x[i] + (dx[i] / d) * m));
      y[i] = Math.min(HEIGHT - 30, Math.max(30, y[i] + (dy[i] / d) * m));
    }
  }
  return {
    nodes: ids.map((id, i) => ({ id, x: x[i], y: y[i], indegree: importers.get(id).size })),
    links,
    shown: n,
    total: files.size,
  };
}

export function shortName(path) {
  const parts = path.split("/");
  return parts.length > 2 ? parts.slice(-2).join("/") : path;
}
