import { describe, expect, it } from "vitest";
import { blastCone, diffSeeds, fileGraph, layout } from "./graph";

const f = (id, path) => ({ id, label: "File", properties: { path } });
const imp = (a, b) => ({ id: `${a}>${b}`, type: "IMPORTS", source_id: a, target_id: b, properties: {} });
const sample = {
  nodes: [f("a", "pkg/a.py"), f("b", "pkg/b.py"), f("c", "pkg/c.py"), f("d", "d.py"),
    { id: "fn", label: "Function", properties: { qualified_name: "run", line_start: 3 } }],
  relationships: [imp("b", "a"), imp("c", "b"), imp("c", "b"), imp("a", "a"), { id: "x", type: "CONTAINS", source_id: "a", target_id: "fn", properties: {} }],
};

describe("code graph helpers", () => {
  it("keeps files, de-duplicates imports, ignores self-imports and assigns definitions", () => {
    const g = fileGraph(sample);
    expect(g.files.size).toBe(4);
    expect(g.edges).toHaveLength(2);
    expect(g.importers.get("a")).toEqual(new Set(["b"]));
    expect(g.files.get("a").defs).toEqual([{ kind: "function", name: "run", line: 3 }]);
  });

  it("the blast cone is every file that depends on the seeds, directly or not", () => {
    const g = fileGraph(sample);
    expect(blastCone(new Set(["a"]), g.importers)).toEqual(new Set(["b", "c"]));
    expect(blastCone(new Set(["d"]), g.importers).size).toBe(0);
  });

  it("finds seeds from a diff, by exact or suffix path, and reports unknown files", () => {
    const g = fileGraph(sample);
    const diff = "diff --git a/src/pkg/a.py b/src/pkg/a.py\n--- a/src/pkg/a.py\n+++ b/src/pkg/a.py\n@@ -1 +1 @@\n+x\n--- a/nope.py\n+++ b/nope.py\n";
    const { seeds, missing } = diffSeeds(diff, g.files);
    expect([...seeds]).toEqual(["a"]);
    expect(missing).toEqual(["nope.py"]);
  });

  it("lays out deterministically inside the canvas and caps the node count", () => {
    const g = fileGraph(sample);
    const one = layout(g.files, g.edges, g.importers, g.imports);
    const two = layout(g.files, g.edges, g.importers, g.imports);
    expect(one).toEqual(two);
    expect(one.nodes.every((n) => n.x >= 0 && n.x <= 1000 && n.y >= 0 && n.y <= 640)).toBe(true);
    expect(layout(g.files, g.edges, g.importers, g.imports, { max: 2 }).shown).toBe(2);
  });
});
