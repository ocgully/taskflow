// Flow-network canvas — HW-0042.
//
// Rewrite of the original HW-0029 canvas on top of React Flow v12
// (@xyflow/react), loaded via esm.sh with preact/compat as the React
// interop layer. No bundler; no npm. Layout comes from elkjs
// (layered algorithm), which handles source/sink ordering natively.
//
// What this file owns:
//   * Shape per executor component (agent/gate/service/target/queue/
//     source/group) rendered as custom React Flow node types — each
//     exposing left (target) + right (source) Handles so edges
//     terminate on handle anchors, not on hand-rolled rect bounds.
//   * elkjs LR layout: source-only nodes (inbox) pin to layer 0,
//     sink-only nodes (archived, prod-deploy) pin to the last layer.
//   * Live SSE: subscribe to /api/events, translate flow.push/ack/
//     enter/leave into packet-dot animations on the connecting edge.
//   * Journey overlay: when `journeyId` prop is set, fetch
//     /api/items/{id}/journey and highlight the visited edges/nodes.
//   * Node + edge click -> inline right-dock detail (.fx-detail).
//
// What this file intentionally does NOT own (from the HW-0029
// version that we replaced):
//   * Hand-rolled dagre + raw SVG — React Flow does this now.
//   * Drag-to-reposition + layout persistence — explicitly removed
//     (nodesDraggable=false). UAT #9: no drag-to-reposition.
//   * The edge-trim hack — React Flow's Handle system terminates
//     edges at defined anchors so this is unnecessary.

import { h, Fragment } from "https://esm.sh/preact@10.22.0";
import {
  useState, useEffect, useMemo, useCallback,
} from "https://esm.sh/preact@10.22.0/hooks";

// React Flow v12 via esm.sh, aliasing react/react-dom -> preact/compat.
// The deps=preact pin ensures esm.sh resolves the SAME preact instance
// the rest of the app uses (otherwise hooks from two preact copies
// would cross-talk and crash). react.mjs is the compat build — it
// exposes the React-named exports React Flow's source imports.
const XYFLOW_URL =
  "https://esm.sh/@xyflow/react@12.3.6" +
  "?alias=react:preact/compat,react-dom:preact/compat" +
  "&deps=preact@10.22.0";

const {
  ReactFlow,
  ReactFlowProvider,
  Controls,
  MiniMap,
  Background,
  BackgroundVariant,
  Handle,
  Position,
  useReactFlow,
} = await import(XYFLOW_URL);

// Load React Flow's stylesheet exactly once. `<link>` is cheaper than
// fetching + inlining; browsers dedupe by href so module re-imports
// don't produce duplicate sheets.
(function ensureRfStylesheet() {
  const href = "https://esm.sh/@xyflow/react@12.3.6/dist/style.css";
  if (document.querySelector(`link[data-rfstyle][href="${href}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = href;
  link.setAttribute("data-rfstyle", "1");
  document.head.appendChild(link);
})();

// elkjs — deterministic layered layout with proper sink/source ranks.
// The `bundled` build ships the worker inline so no worker-path config.
const ELK = (await import("https://esm.sh/elkjs@0.9.3/lib/elk.bundled.js")).default;
const elk = new ELK();

// ---------------------------------------------------------------------------
// Component-type -> color + priority. Mirrors the HW-0029 palette so
// legend + minimap swatches stay familiar.
// ---------------------------------------------------------------------------

const COMPONENT_HUE = {
  agent:               { base: "#6ea8ff", accent: "#6ea8ff" },
  gate:                { base: "#f5b556", accent: "#f5b556" },
  "approval-gate":     { base: "#f5b556", accent: "#f5b556" },
  service:             { base: "#5bd49b", accent: "#5bd49b" },
  target:              { base: "#a17aff", accent: "#a17aff" },
  "deployment-target": { base: "#a17aff", accent: "#a17aff" },
  source:              { base: "#8a93a2", accent: "#8a93a2" },
  queue:               { base: "#7fd4d4", accent: "#7fd4d4" },
  group:               { base: "#2b303a", accent: "#8a93a2" },
};

const COMPONENT_PRIORITY = [
  "group", "source", "agent", "gate", "approval-gate", "queue",
  "service", "target", "deployment-target",
];

function dominantKind(components) {
  const set = new Set(components || []);
  for (const k of COMPONENT_PRIORITY) if (set.has(k)) return k;
  return (components && components[0]) || "service";
}
function hueFor(kind) {
  return COMPONENT_HUE[kind] || { base: "#6ea8ff", accent: "#6ea8ff" };
}

// Agent-initials for the avatar badge. "@engineer" -> "EN".
function initials(id) {
  const s = String(id || "?").replace(/^@/, "").replace(/[-_]/g, " ");
  const parts = s.split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

function sizeFor(kind, label) {
  const labelLen = String(label || "").length;
  const base = Math.max(150, 70 + labelLen * 7);
  const wide = Math.max(170, 90 + labelLen * 7);
  switch (kind) {
    case "agent":              return { width: base, height: 52 };
    case "gate":
    case "approval-gate":      return { width: 130, height: 90 };
    case "service":            return { width: base, height: 54 };
    case "target":
    case "deployment-target":  return { width: wide, height: 58 };
    case "queue":              return { width: base, height: 56 };
    case "source":             return { width: base, height: 50 };
    case "group":              return { width: base, height: 50 };
    default:                   return { width: base, height: 52 };
  }
}

// Deterministic unique color per executor id. Hashes id to an HSL hue
// and uses golden-ratio-conjugate mixing to ensure visually distinct
// colors across arbitrary numbers of agents (no "next available slot"
// limit — 20 agents are all far apart on the wheel).
function hueForId(id) {
  let h = 2166136261 >>> 0;
  const s = String(id || "");
  for (let i = 0; i < s.length; i++) {
    h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  }
  const unit = (h >>> 0) / 4294967295;
  return Math.floor((unit + 0.618033988749895) * 360) % 360;
}
function uniqueColorFor(id) {
  return `hsl(${hueForId(id)}, 70%, 65%)`;
}
function uniqueColorDimFor(id) {
  return `hsl(${hueForId(id)}, 55%, 50%)`;
}

// Tint base-color by saturation (0..1) toward --bg-3.
function mixWithBg(hex, sat) {
  const s = Math.max(0, Math.min(1, sat || 0.2));
  const t = 0.18 + s * 0.57;
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  const bgR = 0x24, bgG = 0x28, bgB = 0x31;
  const mix = (c, bc) => Math.round(bc + (c - bc) * t);
  return `rgb(${mix(r, bgR)}, ${mix(g, bgG)}, ${mix(b, bgB)})`;
}

// ---------------------------------------------------------------------------
// Custom node components. Each one exposes a left (target) handle and a
// right (source) handle — that's the whole point of this rewrite,
// because it's what makes edges terminate on defined anchors instead
// of floating toward a rect boundary we compute ourselves.
//
// React Flow measures the node's DOM box for layout, so these wrap a
// fixed-size div whose background is an inline SVG for non-rect
// shapes (diamond/hexagon/document/parallelogram). The SVG is purely
// visual; the outer div owns the hit region and handle coordinates.
// ---------------------------------------------------------------------------

const HANDLE_STYLE = {
  width: 6,
  height: 6,
  background: "transparent",
  border: "none",
  opacity: 0,
};

// Five handles per side, positioned at y = 15/32.5/50/67.5/85 %. Edges
// pick a handle based on the target's vertical offset from this node's
// centre, so outgoing routes fan out vertically instead of stacking on
// a single anchor point.
const HANDLE_OFFSETS = [0.15, 0.325, 0.5, 0.675, 0.85];
function makeHandles(accent) {
  const base = { ...HANDLE_STYLE, borderColor: accent };
  const nodes = [];
  HANDLE_OFFSETS.forEach((off, i) => {
    nodes.push(h(Handle, {
      key: `t${i}`,
      type: "target",
      position: Position.Left,
      id: `t${i}`,
      style: { ...base, top: `${off * 100}%` },
      isConnectable: false,
    }));
    nodes.push(h(Handle, {
      key: `s${i}`,
      type: "source",
      position: Position.Right,
      id: `s${i}`,
      style: { ...base, top: `${off * 100}%` },
      isConnectable: false,
    }));
  });
  return h(Fragment, null, ...nodes);
}
// Keep the old name alive so existing call sites in the file work.
const handles = makeHandles;

// Agent: rounded-rect card with a circular avatar (initials).
function AgentNode({ data, selected }) {
  const { executor, saturation, depth, active, width, height, highlighted } = data;
  const { base, accent } = hueFor("agent");
  const fill = mixWithBg(base, saturation);
  const uniqueColor = uniqueColorFor(executor.id);
  const stroke = selected || highlighted ? "#ffffff" : accent;
  const total = (depth || 0) + (active || 0);
  return h("div", {
    class: "fx-rf-node fx-rf-agent" + (selected ? " fx-selected" : "") +
           (highlighted ? " fx-highlighted" : ""),
    style: {
      width: width + "px",
      height: height + "px",
      background: fill,
      border: `${selected ? 2.2 : 1.4}px solid ${stroke}`,
      borderRadius: "12px",
      position: "relative",
      color: "var(--fg)",
      display: "flex", alignItems: "center", gap: "8px", padding: "0 12px 0 8px",
    },
  },
    h("div", { class: "fx-rf-avatar",
      style: { borderColor: uniqueColor, color: uniqueColor, borderWidth: "2px" } },
      initials(executor.id)),
    h("div", { class: "fx-rf-label" }, executor.label || executor.id),
    total > 0 && h("div", { class: "fx-rf-badge" }, String(total)),
    handles(accent),
  );
}

// Generic shape helper — renders a polygon/path as SVG-background so
// the node's hit region remains a plain rectangle React Flow can size.
function ShapeNode({ data, selected, kind, polygon, pathD }) {
  const { executor, saturation, depth, active, width, height, highlighted } = data;
  const { base, accent } = hueFor(kind);
  const fill = mixWithBg(base, saturation);
  const stroke = selected || highlighted ? "#ffffff" : accent;
  const strokeW = selected ? 2.2 : 1.4;
  const total = (depth || 0) + (active || 0);

  const svg = h("svg", {
    width, height, viewBox: `0 0 ${width} ${height}`,
    style: { position: "absolute", inset: 0, pointerEvents: "none" },
  },
    polygon && h("polygon", { points: polygon(width, height),
      fill, stroke, "stroke-width": strokeW }),
    pathD && h("path", { d: pathD(width, height),
      fill, stroke, "stroke-width": strokeW }),
  );

  return h("div", {
    class: `fx-rf-node fx-rf-${kind}` + (selected ? " fx-selected" : "") +
           (highlighted ? " fx-highlighted" : ""),
    style: {
      width: width + "px",
      height: height + "px",
      position: "relative",
      display: "flex", alignItems: "center", justifyContent: "center",
      color: "var(--fg)",
    },
  },
    svg,
    h("div", { class: "fx-rf-label", style: { position: "relative", zIndex: 1 } },
      executor.label || executor.id),
    total > 0 && h("div", { class: "fx-rf-badge" }, String(total)),
    handles(accent),
  );
}

// Diamond (gate / approval-gate).
function diamondPoints(w, h) {
  const hw = w / 2, hh = h / 2;
  return `${hw},2 ${w - 2},${hh} ${hw},${h - 2} 2,${hh}`;
}
// Hexagon (service) — flat left/right.
function hexagonPoints(w, h) {
  const inset = Math.min(16, w * 0.18);
  return `${inset},2 ${w - inset},2 ${w - 2},${h / 2} ${w - inset},${h - 2} ${inset},${h - 2} 2,${h / 2}`;
}
// Parallelogram (source).
function parallelogramPoints(w, h) {
  const skew = 14;
  return `${skew},2 ${w - 2},2 ${w - skew},${h - 2} 2,${h - 2}`;
}
// Document (target/deployment-target) — rect with a wavy bottom edge.
function documentPath(w, h) {
  const wave = 7;
  return [
    `M 2 2`,
    `H ${w - 2}`,
    `V ${h - wave}`,
    `C ${w * 0.78} ${h + wave / 2} ${w * 0.58} ${h - wave * 1.5} ${w / 2} ${h - wave}`,
    `C ${w * 0.42} ${h - wave / 2} ${w * 0.22} ${h + wave} 2 ${h - wave}`,
    "Z",
  ].join(" ");
}

function GateNode(p)    { return ShapeNode({ ...p, kind: "gate",    polygon: diamondPoints       }); }
function ServiceNode(p) { return ShapeNode({ ...p, kind: "service", polygon: hexagonPoints       }); }
function SourceNode(p)  { return ShapeNode({ ...p, kind: "source",  polygon: parallelogramPoints }); }
function TargetNode(p)  { return ShapeNode({ ...p, kind: "target",  pathD:   documentPath        }); }

// Queue: stack of three offset rounded-rects.
function QueueNode({ data, selected }) {
  const { executor, saturation, depth, active, width, height, highlighted } = data;
  const { base, accent } = hueFor("queue");
  const fill = mixWithBg(base, saturation);
  const stroke = selected || highlighted ? "#ffffff" : accent;
  const strokeW = selected ? 2.2 : 1.4;
  const total = (depth || 0) + (active || 0);
  const rect = (dx, dy, opacity) => h("rect", {
    x: dx, y: dy, width: width - 8, height: height - 8,
    rx: 4, ry: 4,
    fill: opacity ? accent : fill, opacity: opacity || 1,
    stroke: opacity ? "none" : stroke, "stroke-width": strokeW,
  });
  return h("div", {
    class: "fx-rf-node fx-rf-queue" + (selected ? " fx-selected" : "") +
           (highlighted ? " fx-highlighted" : ""),
    style: {
      width: width + "px", height: height + "px", position: "relative",
      display: "flex", alignItems: "center", justifyContent: "center",
      color: "var(--fg)",
    },
  },
    h("svg", {
      width, height, viewBox: `0 0 ${width} ${height}`,
      style: { position: "absolute", inset: 0, pointerEvents: "none" },
    },
      rect(8, 8, 0.35),
      rect(4, 4, 0.60),
      rect(0, 0, 0),
    ),
    h("div", { class: "fx-rf-label", style: { position: "relative", zIndex: 1 } },
      executor.label || executor.id),
    total > 0 && h("div", { class: "fx-rf-badge" }, String(total)),
    handles(accent),
  );
}

// Group: dashed outline container (no internal composition — we
// visualise the group as a peer node, children render alongside).
function GroupNode({ data, selected }) {
  const { executor, width, height, highlighted } = data;
  const { accent } = hueFor("group");
  return h("div", {
    class: "fx-rf-node fx-rf-group" + (selected ? " fx-selected" : "") +
           (highlighted ? " fx-highlighted" : ""),
    style: {
      width: width + "px", height: height + "px",
      borderRadius: "8px",
      border: `1.4px dashed ${accent}`,
      background: "rgba(138, 147, 162, 0.05)",
      display: "flex", alignItems: "center", justifyContent: "center",
      color: "var(--fg)",
    },
  },
    h("div", { class: "fx-rf-label" }, executor.label || executor.id),
    handles(accent),
  );
}

const NODE_TYPES = {
  agent:               AgentNode,
  gate:                GateNode,
  "approval-gate":     GateNode,
  service:             ServiceNode,
  source:              SourceNode,
  queue:               QueueNode,
  target:              TargetNode,
  "deployment-target": TargetNode,
  group:               GroupNode,
};

// ---------------------------------------------------------------------------
// ELK layout — layered LR with source/sink rank pinning.
// Elk's `elk.layered` natively respects `layerConstraint: FIRST`/`LAST`,
// which is what we need to force inbox leftmost + archived/prod-deploy
// rightmost (UAT #3).
// ---------------------------------------------------------------------------

async function computeLayout(executors, routes) {
  const ids = new Set(executors.map((e) => e.id));

  // --- Back-edge detection via DFS --------------------------------------
  // An edge is a "back edge" iff it targets a node currently on the DFS
  // stack (an ancestor). Back edges close cycles; removing them yields a
  // DAG that ELK can layer cleanly. We also use this classification to
  // render forward edges white and back edges grey + dashed.
  const adj = new Map();
  for (const ex of executors) adj.set(ex.id, []);
  const edgeIndex = new Map();   // `${from}|${to}` -> route index
  routes.forEach((r, i) => {
    if (!ids.has(r.from) || !ids.has(r.to)) return;
    adj.get(r.from).push(r.to);
    if (!edgeIndex.has(`${r.from}|${r.to}`)) edgeIndex.set(`${r.from}|${r.to}`, i);
  });
  const WHITE = 0, GRAY = 1, BLACK = 2;
  const color = new Map();
  for (const ex of executors) color.set(ex.id, WHITE);
  const backEdges = new Set();   // `${from}|${to}`

  const dfs = (u) => {
    color.set(u, GRAY);
    for (const v of adj.get(u) || []) {
      const c = color.get(v);
      if (c === WHITE) dfs(v);
      else if (c === GRAY) backEdges.add(`${u}|${v}`);
      // BLACK: forward/cross edge — not a back edge.
    }
    color.set(u, BLACK);
  };

  // Source-only nodes first so DFS roots sit at the flow origin.
  const sourceSeeds = executors.filter((e) =>
    !routes.some((r) => r.to === e.id && ids.has(r.from)));
  for (const s of sourceSeeds) if (color.get(s.id) === WHITE) dfs(s.id);
  for (const ex of executors) if (color.get(ex.id) === WHITE) dfs(ex.id);

  // Recompute in/out counts over the FORWARD DAG only (ignore back edges),
  // so `LAST_SEPARATE` actually catches the real sinks (archived, prod-deploy)
  // even when cross-cutters back-link to them.
  const incoming = new Map();
  const outgoing = new Map();
  for (const r of routes) {
    if (!ids.has(r.from) || !ids.has(r.to)) continue;
    if (backEdges.has(`${r.from}|${r.to}`)) continue;
    incoming.set(r.to, (incoming.get(r.to) || 0) + 1);
    outgoing.set(r.from, (outgoing.get(r.from) || 0) + 1);
  }

  const graph = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.layered.spacing.nodeNodeBetweenLayers": "140",
      "elk.layered.spacing.edgeNodeBetweenLayers": "60",
      "elk.layered.spacing.edgeEdgeBetweenLayers": "24",
      "elk.spacing.nodeNode": "56",
      "elk.spacing.edgeNode": "28",
      "elk.spacing.edgeEdge": "18",
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
      "elk.layered.crossingMinimization.semiInteractive": "true",
    },
    children: executors.map((ex) => {
      const kind = dominantKind(ex.components);
      const label = ex.label || ex.id;
      const { width, height } = sizeFor(kind, label);
      const hasIn  = (incoming.get(ex.id) || 0) > 0;
      const hasOut = (outgoing.get(ex.id) || 0) > 0;
      const layoutOptions = {};
      if (!hasIn  && hasOut) layoutOptions["elk.layered.layering.layerConstraint"] = "FIRST_SEPARATE";
      if ( hasIn && !hasOut) layoutOptions["elk.layered.layering.layerConstraint"] = "LAST_SEPARATE";
      return { id: ex.id, width, height, layoutOptions };
    }),
    edges: routes
      .filter((r) => ids.has(r.from) && ids.has(r.to))
      .filter((r) => !backEdges.has(`${r.from}|${r.to}`))
      .map((r, i) => ({
        id: `e${i}-${r.from}-${r.to}`,
        sources: [r.from],
        targets: [r.to],
      })),
  };

  const res = await elk.layout(graph);

  // Diagonal shear — bias later-in-flow nodes further DOWN as well as
  // right, so top-left = flow origin, bottom-right = flow terminus.
  // Each rank's x translates into an additional y offset; within a
  // rank nodes retain their relative vertical ordering but the whole
  // column slides down.
  const DIAGONAL_SLOPE = 0.35;
  let minX = Infinity;
  for (const c of res.children || []) minX = Math.min(minX, c.x || 0);
  if (!isFinite(minX)) minX = 0;

  const positions = {};
  for (const c of res.children || []) {
    const dx = (c.x || 0) - minX;
    positions[c.id] = {
      x: c.x || 0,
      y: (c.y || 0) + dx * DIAGONAL_SLOPE,
      width: c.width,
      height: c.height,
    };
  }
  const totalHeight = (res.height || 600) + (res.width || 800) * DIAGONAL_SLOPE;
  return { positions, width: res.width || 800, height: totalHeight, backEdges };
}

// ---------------------------------------------------------------------------
// Data fetchers — unchanged from HW-0029; server contract is stable.
// ---------------------------------------------------------------------------

async function fetchNetwork() {
  const r = await fetch("/api/network");
  if (!r.ok) throw new Error(`/api/network -> ${r.status}`);
  return r.json();
}
async function fetchPackets() {
  const r = await fetch("/api/packets");
  if (!r.ok) throw new Error(`/api/packets -> ${r.status}`);
  return r.json();
}
async function fetchJourney(id) {
  const r = await fetch(`/api/items/${encodeURIComponent(id)}/journey`);
  if (!r.ok) throw new Error(`/api/items/${id}/journey -> ${r.status}`);
  return r.json();
}

// Duration (ms) a packet-dot rides an edge for on a flow.push event.
const PACKET_DURATION_MS = 2400;

// ---------------------------------------------------------------------------
// Inner canvas — rendered inside <ReactFlowProvider> so it can use
// useReactFlow() (fitView, etc).
// ---------------------------------------------------------------------------

function InnerCanvas({ onSelect, journeyId, journeyBus,
                       network, layout, packets, setPackets,
                       packetDots, setPacketDots, paused, setPaused,
                       journey, error }) {
  const [selected, setSelected] = useState(null);
  const [tickFrame, setTickFrame] = useState(0);
  const rf = useReactFlow();

  // Journey highlight sets.
  const journeyEdges = useMemo(() => {
    const s = new Set();
    if (!journey || !journey.visited || journey.visited.length < 2) return s;
    for (let i = 0; i < journey.visited.length - 1; i++) {
      s.add(`${journey.visited[i]}|${journey.visited[i + 1]}`);
    }
    return s;
  }, [journey]);
  const journeyNodes = useMemo(() => new Set(journey?.visited || []), [journey]);

  // Per-executor saturation from current packet state.
  const activityByExec = useMemo(() => {
    const raw = {};
    let peak = 1;
    for (const [eid, slot] of Object.entries(packets.by_executor || {})) {
      const v = (slot.inbox_depth || 0) + (slot.active_depth || 0);
      if (v > peak) peak = v;
      raw[eid] = v;
    }
    const sat = {};
    for (const [eid, v] of Object.entries(raw)) {
      sat[eid] = v === 0 ? 0.2 : Math.min(1, 0.35 + 0.65 * (v / peak));
    }
    return { sat, raw };
  }, [packets]);

  // Build React Flow nodes from network + layout.
  const rfNodes = useMemo(() => {
    if (!network || !layout) return [];
    return network.executors.map((ex) => {
      const pos = layout.positions[ex.id] || { x: 0, y: 0, width: 180, height: 52 };
      const kind = dominantKind(ex.components);
      const slot = packets.by_executor[ex.id] || {};
      return {
        id: ex.id,
        type: kind,
        position: { x: pos.x, y: pos.y },
        draggable: false,
        selectable: true,
        data: {
          executor: ex,
          kind,
          width: pos.width,
          height: pos.height,
          saturation: activityByExec.sat[ex.id] || 0.2,
          depth: slot.inbox_depth || 0,
          active: slot.active_depth || 0,
          highlighted: journeyNodes.has(ex.id),
        },
      };
    });
  }, [network, layout, packets, activityByExec, journeyNodes]);

  // Build React Flow edges from routes.
  //
  // Edge colour semantics (HW-0042 follow-up):
  //   forward  -> source's unique hue (required thicker than optional)
  //   backward -> grey (dashed; feedback / rework / cycle-closers)
  //   forbidden-> red  (dashed; DMZ violation — reserved)
  //   highlighted (journey overlay) -> amber, always on top
  //
  // Handle assignment: each node has 5 source handles (right side) and
  // 5 target handles (left side). For each node, we group its outgoing
  // edges and sort them by target's Y position, then distribute across
  // the 5 source handles in order. Same on the target side (sort
  // incoming edges by source's Y, distribute across 5 target handles).
  // This guarantees no two edges share a handle on either end (up to 5
  // per side) and produces a clean fan-out that mimics cable-routing.
  const rfEdges = useMemo(() => {
    if (!network || !layout) return [];
    const backEdges = layout.backEdges || new Set();
    const pos = layout.positions;

    // Per-node outgoing/incoming edges with centroid-y of the opposite end.
    const outgoing = new Map();   // sourceId -> [{key, idx, targetCy}]
    const incoming = new Map();   // targetId -> [{key, idx, sourceCy}]
    network.routes.forEach((r, idx) => {
      const key = `${r.from}|${r.to}`;
      const sp = pos[r.from];
      const tp = pos[r.to];
      if (!sp || !tp) return;
      const sy = sp.y + sp.height / 2;
      const ty = tp.y + tp.height / 2;
      if (!outgoing.has(r.from)) outgoing.set(r.from, []);
      outgoing.get(r.from).push({ key, idx, oppY: ty });
      if (!incoming.has(r.to)) incoming.set(r.to, []);
      incoming.get(r.to).push({ key, idx, oppY: sy });
    });

    const sourceHandleFor = new Map();   // edgeKey -> `s${N}`
    const targetHandleFor = new Map();   // edgeKey -> `t${N}`
    const assign = (map, entries, prefix) => {
      const sorted = entries.slice().sort((a, b) => a.oppY - b.oppY);
      const n = sorted.length;
      const slots = HANDLE_OFFSETS.length;
      sorted.forEach((e, i) => {
        // Spread across available handles deterministically.
        const slot = n === 1 ? Math.floor(slots / 2)
                             : Math.round((i * (slots - 1)) / (n - 1));
        map.set(e.key, `${prefix}${slot}`);
      });
    };
    for (const [src, edges] of outgoing) assign(sourceHandleFor, edges, "s");
    for (const [tgt, edges] of incoming) assign(targetHandleFor, edges, "t");

    return network.routes.map((r, i) => {
      const route = r || {};
      const required = !!route.required;
      const forbidden = !!route.forbidden;
      const cond = route.condition;
      const key = `${route.from}|${route.to}`;
      const hl = journeyEdges.has(key);
      const isBack = backEdges.has(key);

      let cls = "fx-rf-edge";
      if (isBack) cls += " fx-edge-back";
      else if (required) cls += " fx-edge-required";
      else if (cond) cls += " fx-edge-conditional";
      else cls += " fx-edge-optional";
      if (forbidden) cls += " fx-edge-forbidden";
      if (hl) cls += " fx-edge-highlighted";

      // Color resolution (highlight > forbidden > back > forward-by-source).
      // Forward edges take the SOURCE executor's unique color — this
      // makes it easy to trace what comes out of each agent (NYC-subway
      // style) and keeps the agent's avatar ring visually paired with
      // its outgoing edges.
      const stroke =
        hl ? "#f5b556" :
        forbidden ? "#ff6b6b" :
        isBack ? "#8a93a2" :
        uniqueColorFor(route.from);
      const strokeWidth =
        hl ? 3 :
        isBack ? 1.4 :
        required ? 2.4 :
        1.8;
      const strokeDasharray =
        forbidden ? "6 4" :
        isBack ? "6 5" :
        undefined;

      return {
        id: `e${i}-${route.from}-${route.to}`,
        source: route.from,
        target: route.to,
        sourceHandle: sourceHandleFor.get(key),
        targetHandle: targetHandleFor.get(key),
        type: "smoothstep",
        animated: false,
        className: cls,
        label: cond || undefined,
        labelBgStyle: cond ? { fill: "var(--bg-3)" } : undefined,
        labelStyle:   cond ? { fill: "var(--fg)", fontFamily: "ui-monospace, monospace", fontSize: 10 } : undefined,
        markerEnd: forbidden ? undefined : {
          type: "arrowclosed",
          color: stroke,
          width: hl ? 16 : 14,
          height: hl ? 16 : 14,
        },
        style: { stroke, strokeWidth, strokeDasharray },
        data: { route, key, isBack },
      };
    });
  }, [network, layout, journeyEdges]);

  // RAF pulse while any packet is in flight, so we re-render position
  // frames. React Flow edges are the stable substrate; the packets are
  // absolutely-positioned overlay dots computed from the edge's DOM.
  useEffect(() => {
    if (paused || packetDots.length === 0) return;
    let raf;
    const tick = () => {
      const now = performance.now();
      setPacketDots((prev) => prev.filter((p) => now - p.startedAt < PACKET_DURATION_MS));
      setTickFrame((n) => (n + 1) % 1_000_000);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [paused, packetDots.length]);

  // Packet-dot positions sampled from actual rendered edge <path> DOM
  // (React Flow exposes .react-flow__edge-path per edge). The edge path
  // lives in an SVG inside the .react-flow__viewport transform — so
  // we map SVG-local coords -> screen coords via getScreenCTM(), then
  // screen -> fx-canvas-local via the canvas's bounding rect, so the
  // overlay dots stay locked to the edge at any zoom/pan.
  const renderedDots = useMemo(() => {
    if (packetDots.length === 0) return [];
    const now = performance.now();
    const canvasEl = document.querySelector(".fx-canvas");
    const canvasRect = canvasEl ? canvasEl.getBoundingClientRect() : { left: 0, top: 0 };
    const out = [];
    for (const p of packetDots) {
      const t = Math.min(1, (now - p.startedAt) / PACKET_DURATION_MS);
      const edgeEl = document.querySelector(
        `.react-flow__edge[data-id*="-${p.from}-${p.to}"] .react-flow__edge-path`);
      if (!edgeEl) continue;
      let len = 0;
      try { len = edgeEl.getTotalLength(); } catch { continue; }
      const local = edgeEl.getPointAtLength(t * len);
      const ctm = edgeEl.getScreenCTM();
      if (!ctm) continue;
      // SVGPoint * CTM -> screen; then subtract canvas origin -> canvas-local.
      const screenX = ctm.a * local.x + ctm.c * local.y + ctm.e;
      const screenY = ctm.b * local.x + ctm.d * local.y + ctm.f;
      out.push({
        ...p,
        x: screenX - canvasRect.left,
        y: screenY - canvasRect.top,
      });
    }
    return out;
  // `tickFrame` forces recompute each RAF frame; other deps trigger
  // when packet-set or graph DOM changes.
  }, [packetDots, tickFrame, rfEdges, rfNodes]);

  // Interaction handlers.
  const onNodeClick = useCallback((_e, node) => {
    setSelected({ kind: "executor", id: node.id });
  }, []);
  const onEdgeClick = useCallback((_e, edge) => {
    setSelected({ kind: "edge", id: edge.id, route: edge.data?.route });
  }, []);
  const onPaneClick = useCallback(() => setSelected(null), []);

  if (error) return h("div", { class: "empty", style: "color: var(--err)" }, `Canvas error: ${error}`);
  if (!network) return h("div", { class: "muted" }, "Loading flow network…");
  if (!network.executors || network.executors.length === 0) {
    return h("div", { class: "empty" },
      "No flow network yet. Run `hopewell network init && hopewell network defaults bootstrap`.");
  }
  if (!layout) return h("div", { class: "muted" }, "Computing layout…");

  const detailPanel = renderInlineDetail({
    selected, network, packets, onSelect,
    onJumpItem: (id) => onSelect && onSelect(id),
  });

  return h("div", { class: "fx-canvas", tabIndex: 0 },
    // Toolbar.
    h("div", { class: "fx-toolbar" },
      h("button", { onClick: () => setPaused(!paused),
        title: "Pause/resume packet animation" },
        paused ? "> resume" : "|| pause"),
      h("button", { onClick: () => rf.fitView({ padding: 0.1, duration: 300 }),
        title: "Fit view" }, "fit"),
      h("span", { class: "fx-toolbar-hint muted" },
        `${network.executors.length} nodes | ${network.routes.length} edges`),
      journeyId && h("span", { class: "fx-toolbar-journey" },
        `journey: ${journeyId}`,
        h("button", { class: "fx-toolbar-clear",
          onClick: () => journeyBus && journeyBus(null) }, "x")),
    ),

    // React Flow canvas.
    h(ReactFlow, {
      nodes: rfNodes,
      edges: rfEdges,
      nodeTypes: NODE_TYPES,
      nodesDraggable: false,
      nodesConnectable: false,
      elementsSelectable: true,
      selectNodesOnDrag: false,
      onNodeClick,
      onEdgeClick,
      onPaneClick,
      fitView: true,
      fitViewOptions: { padding: 0.1 },
      proOptions: { hideAttribution: true },
      minZoom: 0.15,
      maxZoom: 3,
      defaultEdgeOptions: { type: "smoothstep" },
    },
      h(Background, { variant: BackgroundVariant.Dots, gap: 18, size: 1, color: "#2b303a" }),
      h(Controls, { showInteractive: false, position: "bottom-left" }),
      h(MiniMap, {
        pannable: true, zoomable: true, position: "bottom-right",
        nodeColor: (n) => hueFor(n.type || "service").base,
        nodeStrokeColor: (n) => hueFor(n.type || "service").accent,
        maskColor: "rgba(20, 22, 26, 0.7)",
        style: { background: "#1b1e24" },
      }),

      // Packet overlay lives INSIDE the React Flow viewport so it pans
      // + zooms with the graph. We sit it in an absolutely-positioned
      // SVG at the same transform, but the simpler approach is to rely
      // on renderedDots being in world coords (getPointAtLength on the
      // screen-space path) and render via a ReactFlow-agnostic overlay
      // that lives outside the <ReactFlow> <defs>.
    ),

    // Packet overlay — SVG layered above the React Flow viewport.
    // Dots come pre-projected into screen space by getPointAtLength
    // against the already-transformed edge <path>, so no transform here.
    h("svg", {
      class: "fx-packet-layer",
      style: {
        position: "absolute", inset: 0,
        width: "100%", height: "100%",
        pointerEvents: "none", zIndex: 5,
      },
    },
      renderedDots.map((p) => {
        // Hash the work-item id for stable per-item colour.
        let hh = 0;
        for (let i = 0; i < p.node.length; i++) hh = (hh * 31 + p.node.charCodeAt(i)) >>> 0;
        const color = `hsl(${hh % 360}, 70%, 65%)`;
        return h("g", { key: p.key, class: "fx-packet" },
          h("circle", { cx: p.x, cy: p.y, r: 5.5,
            fill: color, stroke: "#14161a", "stroke-width": 1.5 }),
          h("text", { class: "fx-packet-label",
            x: p.x, y: p.y - 10,
            "text-anchor": "middle", fill: color }, p.node),
        );
      }),
    ),

    // Legend.
    h("div", { class: "fx-legend" },
      h("div", { style: "font-weight:600; margin-bottom:4px" }, "component"),
      ["agent", "gate", "service", "target", "queue", "source", "group"].map((k) =>
        h("div", { key: k },
          h("span", { class: "fx-sw", style: `background:${hueFor(k).base}` }),
          k)),
      h("div", { style: "margin-top:6px; color:var(--fg-muted); font-size:10px" },
        "edges: required=bold - optional=thin - conditional=pill - forbidden=red"),
    ),

    // Inline detail panel (right-dock). Stays open across selections.
    detailPanel,
  );
}

// ---------------------------------------------------------------------------
// CanvasView — the exported shell. Owns data + SSE; delegates render
// to InnerCanvas so useReactFlow() is inside a provider.
// ---------------------------------------------------------------------------

export function CanvasView({ onSelect, journeyId, journeyBus }) {
  const [network, setNetwork] = useState(null);
  const [layout, setLayout]   = useState(null);
  const [packets, setPackets] = useState({ by_executor: {}, in_flight: [] });
  const [packetDots, setPacketDots] = useState([]);
  const [paused, setPaused]   = useState(false);
  const [journey, setJourney] = useState(null);
  const [error, setError]     = useState(null);

  // Initial network + packets fetch.
  useEffect(() => {
    let cancel = false;
    fetchNetwork()
      .then(async (n) => {
        if (cancel) return;
        setNetwork(n);
        try {
          const l = await computeLayout(n.executors, n.routes);
          if (!cancel) setLayout(l);
        } catch (e) {
          console.error("canvas layout failed", e);
          if (!cancel) setError(String(e));
        }
      })
      .catch((e) => { if (!cancel) setError(String(e)); });
    fetchPackets()
      .then((p) => { if (!cancel) setPackets(p); })
      .catch(() => {});
    return () => { cancel = true; };
  }, []);

  // SSE subscription -> packet dots + live depth refresh.
  useEffect(() => {
    const es = new EventSource("/api/events");
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        const kind = data.kind || "";
        if (!kind.startsWith("flow.")) return;
        if (paused) return;

        if (kind === "flow.push") {
          const d = data.data || {};
          const from = d.from_executor || null;
          const to = d.to_executor;
          const node = data.node;
          if (from && to && node) {
            setPacketDots((prev) => [...prev, {
              key: `${node}-${data.ts}-${from}-${to}`,
              from, to, node,
              startedAt: performance.now(),
            }]);
          }
        }
        // Refresh depth badges on any flow event.
        fetchPackets().then(setPackets).catch(() => {});
      } catch { /* ignore */ }
    };
    es.onerror = () => { /* EventSource auto-reconnects */ };
    return () => es.close();
  }, [paused]);

  // Journey overlay fetch whenever journeyId changes.
  useEffect(() => {
    if (!journeyId) { setJourney(null); return; }
    let cancel = false;
    fetchJourney(journeyId)
      .then((j) => { if (!cancel) setJourney(j); })
      .catch(() => { if (!cancel) setJourney(null); });
    return () => { cancel = true; };
  }, [journeyId]);

  return h(ReactFlowProvider, null,
    h(InnerCanvas, {
      onSelect, journeyId, journeyBus,
      network, layout, packets, setPackets,
      packetDots, setPacketDots, paused, setPaused,
      journey, error,
    }),
  );
}

// ---------------------------------------------------------------------------
// Inline detail panel (right-dock) — same shape as HW-0029 so the
// layout of the right-hand column doesn't visibly change.
// ---------------------------------------------------------------------------

function renderInlineDetail({ selected, network, packets, onSelect, onJumpItem }) {
  if (!selected) return null;
  if (selected.kind === "executor") {
    const ex = network.executors.find((e) => e.id === selected.id);
    if (!ex) return null;
    const slot = packets.by_executor[ex.id] || { inbox: [], active: [] };
    return h("aside", { class: "fx-detail" },
      h("div", { class: "fx-detail-head" },
        h("span", { class: "fx-detail-kind" }, dominantKind(ex.components)),
        h("span", { class: "fx-detail-id" }, ex.id),
      ),
      h("div", { class: "fx-detail-label" }, ex.label || ex.id),
      h("div", { class: "fx-detail-section" },
        h("div", { class: "fx-detail-heading" }, "components"),
        h("div", { class: "fx-chips" },
          (ex.components || []).map((c) =>
            h("span", { class: "fx-chip", key: c }, c))),
      ),
      h("div", { class: "fx-detail-section" },
        h("div", { class: "fx-detail-heading" },
          `inbox (${slot.inbox_depth || 0})`),
        (slot.inbox || []).length === 0
          ? h("div", { class: "muted" }, "-- empty --")
          : h("ul", { class: "fx-detail-list" },
              slot.inbox.map((p, i) => h("li", { key: i },
                h("a", { href: "#",
                  onClick: (ev) => { ev.preventDefault(); onJumpItem(p.node); } },
                  p.node),
                " ", h("span", { class: "muted" },
                  p.from_executor ? `from ${p.from_executor}` : "(external)"),
              ))),
      ),
      h("div", { class: "fx-detail-section" },
        h("div", { class: "fx-detail-heading" },
          `active at executor (${slot.active_depth || 0})`),
        (slot.active || []).length === 0
          ? h("div", { class: "muted" }, "-- empty --")
          : h("ul", { class: "fx-detail-list" },
              slot.active.map((a, i) => h("li", { key: i },
                h("a", { href: "#",
                  onClick: (ev) => { ev.preventDefault(); onJumpItem(a.node_id); } },
                  a.node_id),
                " ", h("span", null, a.title || ""),
              ))),
      ),
      Object.keys(ex.component_data || {}).length > 0 &&
        h("div", { class: "fx-detail-section" },
          h("div", { class: "fx-detail-heading" }, "component_data"),
          h("pre", { class: "fx-detail-code" },
            JSON.stringify(ex.component_data, null, 2)),
        ),
    );
  }
  if (selected.kind === "edge") {
    const r = selected.route || {};
    return h("aside", { class: "fx-detail" },
      h("div", { class: "fx-detail-head" },
        h("span", { class: "fx-detail-kind" }, "route"),
      ),
      h("div", { class: "fx-detail-label" },
        r.from, " -> ", r.to),
      h("div", { class: "fx-detail-section" },
        h("dl", { class: "fx-kv" },
          h("dt", null, "required"),  h("dd", null, r.required ? "yes" : "no"),
          h("dt", null, "condition"), h("dd", null, r.condition || "--"),
          h("dt", null, "label"),     h("dd", null, r.label || "--"),
          h("dt", null, "created"),   h("dd", null, r.created || "--"),
        ),
      ),
    );
  }
  return null;
}
