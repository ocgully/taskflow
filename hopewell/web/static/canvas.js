// Flow-network canvas — HW-0029.
//
// Renders the Hopewell flow network (executors + routes) as an SVG
// dagre-laid-out graph, with work items visualised as packets: resting
// depth badges on inbox/queue nodes + coloured dots animating along
// edges when flow.push events arrive via SSE.
//
// Philosophy:
//   * Executors are nodes (the map)
//   * WorkItems are packets flowing through the map (the ledger projected)
//   * This is the opposite of the v0.6 canvas, which treated work items
//     as nodes — that was the HW-0023 misconception HW-0029 corrects.
//
// Shape per component:
//   agent         -> rounded rect + avatar badge (initials)
//   gate          -> diamond
//   approval-gate -> diamond
//   service       -> hexagon
//   target        -> document
//   queue         -> stack icon
//   source        -> parallelogram
//   group         -> outline cluster (sub-layout container)
//
// Route styling:
//   required    -> solid bold
//   optional    -> solid normal
//   conditional -> pill-labeled
//   forbidden   -> red dashed with ⊘ (reserved for DMZ rules)
//
// Layout: dagre LR; manual drags persist to .hopewell/network/layout.json
// and override dagre on next reload. "Reset layout" clears overrides.
//
// Zero new deps: dagre comes from esm.sh just like marked/preact.

import { h, Fragment } from "https://esm.sh/preact@10.22.0";
import { useState, useEffect, useMemo, useRef, useCallback }
  from "https://esm.sh/preact@10.22.0/hooks";
import dagre from "https://esm.sh/@dagrejs/dagre@1.1.4";

// ---------------------------------------------------------------------------
// Component-type -> color
// ---------------------------------------------------------------------------

const COMPONENT_HUE = {
  agent:             { base: "#6ea8ff", accent: "#6ea8ff" },   // blue
  gate:              { base: "#f5b556", accent: "#f5b556" },   // amber
  "approval-gate":   { base: "#f5b556", accent: "#f5b556" },
  service:           { base: "#5bd49b", accent: "#5bd49b" },   // green
  target:            { base: "#a17aff", accent: "#a17aff" },   // purple
  "deployment-target": { base: "#a17aff", accent: "#a17aff" },
  source:            { base: "#8a93a2", accent: "#8a93a2" },   // slate
  queue:             { base: "#7fd4d4", accent: "#7fd4d4" },   // teal
  group:             { base: "#2b303a", accent: "#8a93a2" },   // outline-only
};

const COMPONENT_PRIORITY = [
  "group", "source", "agent", "gate", "approval-gate", "queue",
  "service", "target", "deployment-target",
];

// Pick the dominant component type (priority order) — drives shape + hue.
function dominantKind(components) {
  const set = new Set(components || []);
  for (const k of COMPONENT_PRIORITY) {
    if (set.has(k)) return k;
  }
  // Fall back to the first declared.
  return (components && components[0]) || "service";
}

function hueFor(kind) {
  return COMPONENT_HUE[kind] || { base: "#6ea8ff", accent: "#6ea8ff" };
}

// Agent-initials — for the avatar badge. "@engineer" -> "EN".
function initials(id) {
  const s = String(id || "?").replace(/^@/, "").replace(/[-_]/g, " ");
  const parts = s.split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

// ---------------------------------------------------------------------------
// Shape node-sizer — dagre needs width/height; we pick per-shape defaults.
// ---------------------------------------------------------------------------

function sizeFor(kind, label) {
  const labelLen = String(label || "").length;
  const base = Math.max(150, 70 + labelLen * 7);
  const wide = Math.max(170, 90 + labelLen * 7);
  switch (kind) {
    case "agent":            return { width: base, height: 50 };
    case "gate":
    case "approval-gate":    return { width: 130, height: 90 };
    case "service":          return { width: base, height: 52 };
    case "target":
    case "deployment-target":return { width: wide, height: 56 };
    case "queue":            return { width: base, height: 54 };
    case "source":           return { width: base, height: 48 };
    case "group":            return { width: base, height: 48 };
    default:                 return { width: base, height: 50 };
  }
}

// ---------------------------------------------------------------------------
// Shape renderers — each returns SVG path/rect/polygon strings centred on 0,0.
// ---------------------------------------------------------------------------

// Diamond (gate).
function diamondPoints(w, h) {
  const hw = w / 2, hh = h / 2;
  return `0,${-hh} ${hw},0 0,${hh} ${-hw},0`;
}

// Hexagon (service). Regular-ish, with flat left/right.
function hexagonPoints(w, h) {
  const hw = w / 2, hh = h / 2, inset = Math.min(16, w * 0.18);
  return `${-hw + inset},${-hh} ${hw - inset},${-hh} ${hw},0 ${hw - inset},${hh} ${-hw + inset},${hh} ${-hw},0`;
}

// Parallelogram (source).
function parallelogramPoints(w, h) {
  const hw = w / 2, hh = h / 2, skew = 14;
  return `${-hw + skew},${-hh} ${hw},${-hh} ${hw - skew},${hh} ${-hw},${hh}`;
}

// Document shape (target) — a rect with a wavy bottom. We approximate with
// a cubic bezier path so it renders crisp at any zoom.
function documentPath(w, h) {
  const hw = w / 2, hh = h / 2, wave = 7;
  return [
    `M ${-hw} ${-hh}`,
    `H ${hw}`,
    `V ${hh - wave}`,
    `C ${hw * 0.55} ${hh + wave / 2} ${hw * 0.15} ${hh - wave * 1.5} 0 ${hh - wave}`,
    `C ${-hw * 0.15} ${hh - wave / 2} ${-hw * 0.55} ${hh + wave} ${-hw} ${hh - wave}`,
    "Z",
  ].join(" ");
}

// ---------------------------------------------------------------------------
// Layout engine — dagre + persisted overrides.
// ---------------------------------------------------------------------------

function buildLayout(executors, routes, overrides) {
  const g = new dagre.graphlib.Graph({ compound: true });
  g.setGraph({
    rankdir: "LR",
    ranksep: 70,
    nodesep: 28,
    edgesep: 14,
    marginx: 30,
    marginy: 30,
  });
  g.setDefaultEdgeLabel(() => ({}));

  // Add nodes.
  for (const ex of executors) {
    const kind = dominantKind(ex.components);
    const label = ex.label || ex.id;
    const { width, height } = sizeFor(kind, label);
    g.setNode(ex.id, { width, height, kind, label, executor: ex });
  }

  // Parent nesting (groups) — compound graph. Dagre renders group bounds
  // as the bbox of their children in compound mode.
  for (const ex of executors) {
    if (ex.parent) {
      try { g.setParent(ex.id, ex.parent); } catch { /* ignore */ }
    }
  }

  // Edges — we keep only route-edges that have both endpoints present.
  const ids = new Set(executors.map((e) => e.id));
  for (const r of routes) {
    if (!ids.has(r.from) || !ids.has(r.to)) continue;
    g.setEdge(r.from, r.to, { route: r }, `${r.from}|${r.to}|${r.condition || ""}`);
  }

  dagre.layout(g);

  // Apply persisted overrides (if any).
  const positions = {};
  g.nodes().forEach((id) => {
    const n = g.node(id);
    if (!n) return;
    const o = overrides && overrides[id];
    if (o && typeof o.x === "number" && typeof o.y === "number") {
      n.x = o.x;
      n.y = o.y;
    }
    positions[id] = { x: n.x, y: n.y, width: n.width, height: n.height };
  });

  // Collect edges with their dagre-computed waypoints (points array).
  const edges = [];
  g.edges().forEach((e) => {
    const edgeData = g.edge(e);
    const src = g.node(e.v);
    const tgt = g.node(e.w);
    if (!src || !tgt) return;
    // If either endpoint was overridden, dagre's points are stale — fall
    // back to a straight line between centres. Good enough for a small
    // flow network, and the override itself implies the user took charge
    // of positioning.
    let points = edgeData.points;
    const srcOver = overrides && overrides[e.v];
    const tgtOver = overrides && overrides[e.w];
    if (srcOver || tgtOver) {
      points = [{ x: src.x, y: src.y }, { x: tgt.x, y: tgt.y }];
    }
    edges.push({
      from: e.v,
      to: e.w,
      route: edgeData.route,
      points,
    });
  });

  const gr = g.graph();
  return {
    positions,
    edges,
    width: gr.width || 800,
    height: gr.height || 600,
  };
}

// ---------------------------------------------------------------------------
// Edge path — cubic bezier through dagre waypoints.
// ---------------------------------------------------------------------------

function edgePath(points) {
  if (!points || points.length === 0) return "";
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`;
  // Cardinal-ish smoothing through the dagre waypoints.
  let d = `M ${points[0].x} ${points[0].y}`;
  for (let i = 1; i < points.length; i++) {
    const p0 = points[i - 1];
    const p1 = points[i];
    const mx = (p0.x + p1.x) / 2;
    d += ` C ${mx} ${p0.y} ${mx} ${p1.y} ${p1.x} ${p1.y}`;
  }
  return d;
}

// Pre-compute cumulative-length samples on a polyline so packet dots can
// traverse at constant speed regardless of how many bends there are.
function polylineLength(points) {
  let len = 0;
  for (let i = 1; i < points.length; i++) {
    const dx = points[i].x - points[i - 1].x;
    const dy = points[i].y - points[i - 1].y;
    len += Math.sqrt(dx * dx + dy * dy);
  }
  return len;
}

function pointAtT(points, t) {
  // t in [0, 1]. Linear interpolation along the polyline.
  if (!points || points.length === 0) return { x: 0, y: 0 };
  if (points.length === 1) return { x: points[0].x, y: points[0].y };
  const total = polylineLength(points);
  if (total === 0) return { x: points[0].x, y: points[0].y };
  const target = t * total;
  let acc = 0;
  for (let i = 1; i < points.length; i++) {
    const dx = points[i].x - points[i - 1].x;
    const dy = points[i].y - points[i - 1].y;
    const seg = Math.sqrt(dx * dx + dy * dy);
    if (acc + seg >= target) {
      const u = seg === 0 ? 0 : (target - acc) / seg;
      return {
        x: points[i - 1].x + dx * u,
        y: points[i - 1].y + dy * u,
      };
    }
    acc += seg;
  }
  const last = points[points.length - 1];
  return { x: last.x, y: last.y };
}

// ---------------------------------------------------------------------------
// Node rendering — per-shape SVG + label + packet badge.
// ---------------------------------------------------------------------------

function NodeShape({ pos, executor, kind, selected, saturation, depth,
                    active, onClick, onPointerDown }) {
  const { width, height } = pos;
  const { base, accent } = hueFor(kind);
  const fill = kind === "group" ? "transparent" : mixWithBg(base, saturation);
  const stroke = selected ? "#ffffff" : accent;
  const strokeWidth = selected ? 2.2 : 1.4;

  let shape;
  const common = {
    fill,
    stroke,
    "stroke-width": strokeWidth,
    class: "fx-node-shape",
  };

  switch (kind) {
    case "agent":
      shape = h("rect", {
        ...common,
        x: -width / 2, y: -height / 2,
        width, height, rx: 12, ry: 12,
      });
      break;
    case "gate":
    case "approval-gate":
      shape = h("polygon", { ...common, points: diamondPoints(width, height) });
      break;
    case "service":
      shape = h("polygon", { ...common, points: hexagonPoints(width, height) });
      break;
    case "source":
      shape = h("polygon", { ...common, points: parallelogramPoints(width, height) });
      break;
    case "target":
    case "deployment-target":
      shape = h("path", { ...common, d: documentPath(width, height) });
      break;
    case "queue":
      // Stack icon — three layered rects.
      shape = h(Fragment, null,
        h("rect", { ...common, x: -width / 2 + 4, y: -height / 2 + 4,
          width, height, rx: 4, ry: 4, opacity: 0.35, fill: accent, stroke: "none" }),
        h("rect", { ...common, x: -width / 2 + 2, y: -height / 2 + 2,
          width, height, rx: 4, ry: 4, opacity: 0.6, fill: accent, stroke: "none" }),
        h("rect", { ...common, x: -width / 2, y: -height / 2,
          width, height, rx: 4, ry: 4 }),
      );
      break;
    case "group":
      shape = h("rect", {
        ...common,
        x: -width / 2, y: -height / 2,
        width, height, rx: 8, ry: 8,
        "stroke-dasharray": "6 4",
        fill: "rgba(138, 147, 162, 0.05)",
      });
      break;
    default:
      shape = h("rect", { ...common, x: -width / 2, y: -height / 2,
        width, height, rx: 4, ry: 4 });
  }

  const label = executor.label || executor.id;
  const initialsLabel = kind === "agent" ? initials(executor.id) : null;

  // Total packet count = in-flight arrivals + resting (active locations).
  const total = (depth || 0) + (active || 0);

  return h("g", {
    class: "fx-node" + (selected ? " fx-selected" : ""),
    transform: `translate(${pos.x},${pos.y})`,
    tabIndex: 0,
    role: "button",
    "aria-label": `${kind} ${executor.id}: ${label}`,
    onClick,
    onPointerDown,
  },
    shape,
    initialsLabel && h("g", { class: "fx-avatar", transform: `translate(${-width / 2 + 16}, 0)` },
      h("circle", { r: 13, fill: "#0b0d10", stroke: accent, "stroke-width": 1.2 }),
      h("text", { class: "fx-avatar-text", "text-anchor": "middle",
        "dominant-baseline": "central", fill: accent }, initialsLabel),
    ),
    h("text", {
      class: "fx-node-label",
      "text-anchor": "middle",
      "dominant-baseline": "central",
      x: initialsLabel ? 12 : 0, y: 2,
    }, label),
    total > 0 && h("g", {
      class: "fx-depth-badge",
      transform: `translate(${width / 2 - 4}, ${-height / 2 + 4})`,
    },
      h("circle", { r: 11, fill: "#14161a", stroke: "#f5b556", "stroke-width": 1.4 }),
      h("text", { "text-anchor": "middle", "dominant-baseline": "central",
        fill: "#f5b556", "font-size": 11, "font-weight": 600 }, String(total)),
    ),
  );
}

// Tint a hex color by saturation (0..1) — we linearly blend toward --bg-3.
function mixWithBg(hex, sat) {
  const s = Math.max(0, Math.min(1, sat || 0.2));
  // Minimum tint: 0.18, saturated: 0.75
  const t = 0.18 + s * 0.57;
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  const bgR = 0x24, bgG = 0x28, bgB = 0x31;  // --bg-3 in rgb
  const mix = (c, bc) => Math.round(bc + (c - bc) * t);
  return `rgb(${mix(r, bgR)}, ${mix(g, bgG)}, ${mix(b, bgB)})`;
}

// ---------------------------------------------------------------------------
// Edge rendering — route-kind aware.
// ---------------------------------------------------------------------------

function EdgePath({ edge, highlighted, onClick }) {
  const route = edge.route || {};
  const required = !!route.required;
  const forbidden = !!route.forbidden;      // DMZ — reserved
  const cond = route.condition;

  const d = edgePath(edge.points);

  let cls = "fx-edge";
  if (required) cls += " fx-edge-required";
  else if (cond) cls += " fx-edge-conditional";
  else cls += " fx-edge-optional";
  if (forbidden) cls += " fx-edge-forbidden";
  if (highlighted) cls += " fx-edge-highlighted";

  // Pill label for conditional routes.
  const labelPoint = pointAtT(edge.points, 0.5);
  const labelText = cond || route.label || null;

  return h(Fragment, null,
    h("path", {
      d, class: cls,
      fill: "none",
      "marker-end": forbidden ? null : (highlighted ? "url(#fx-arrow-hl)" :
        (required ? "url(#fx-arrow-bold)" : "url(#fx-arrow)")),
      onClick,
    }),
    // Invisible thicker hit-area for easier clicks.
    h("path", {
      d, class: "fx-edge-hit",
      fill: "none",
      stroke: "transparent",
      "stroke-width": 14,
      onClick,
    }),
    forbidden && h("text", {
      class: "fx-edge-forbidden-mark",
      x: labelPoint.x, y: labelPoint.y,
      "text-anchor": "middle", "dominant-baseline": "central",
    }, "⊘"),
    labelText && !forbidden && h("g", { class: "fx-edge-label",
      transform: `translate(${labelPoint.x}, ${labelPoint.y})` },
      h("rect", { class: "fx-edge-pill",
        x: -labelText.length * 3.2 - 6, y: -8,
        width: labelText.length * 6.4 + 12, height: 16, rx: 8, ry: 8 }),
      h("text", { "text-anchor": "middle", "dominant-baseline": "central" }, labelText),
    ),
  );
}

// ---------------------------------------------------------------------------
// Minimap
// ---------------------------------------------------------------------------

function Minimap({ layout, viewBox, executors, onJumpTo }) {
  if (!layout) return null;
  const pad = 10;
  const vbW = Math.max(100, layout.width + pad * 2);
  const vbH = Math.max(60, layout.height + pad * 2);
  const W = 180;
  const H = Math.max(60, Math.round(W * (vbH / vbW)));

  const kindById = new Map();
  for (const ex of executors || []) {
    kindById.set(ex.id, dominantKind(ex.components));
  }

  return h("svg", {
    class: "fx-minimap",
    width: W, height: H,
    viewBox: `${-pad} ${-pad} ${vbW} ${vbH}`,
    preserveAspectRatio: "xMidYMid meet",
    onClick: (e) => {
      const rect = e.currentTarget.getBoundingClientRect();
      const mx = ((e.clientX - rect.left) / rect.width) * vbW - pad;
      const my = ((e.clientY - rect.top) / rect.height) * vbH - pad;
      onJumpTo && onJumpTo(mx, my);
    },
  },
    h("rect", { x: -pad, y: -pad, width: vbW, height: vbH, fill: "#1b1e24" }),
    Object.entries(layout.positions).map(([id, p]) => {
      const kind = kindById.get(id) || "service";
      const { accent } = hueFor(kind);
      return h("rect", {
        key: id,
        x: p.x - p.width / 2, y: p.y - p.height / 2,
        width: p.width, height: p.height,
        fill: "#2b303a", stroke: accent || "#6ea8ff", "stroke-width": 1,
        rx: 2, ry: 2,
      });
    }),
    viewBox && h("rect", {
      class: "fx-minimap-viewport",
      x: viewBox.x, y: viewBox.y,
      width: viewBox.w, height: viewBox.h,
      fill: "rgba(110, 168, 255, 0.15)",
      stroke: "#6ea8ff", "stroke-width": 1.5,
    }),
  );
}

// ---------------------------------------------------------------------------
// Root Canvas view — the exported Preact component.
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

async function postLayout(positions) {
  const r = await fetch("/api/network/layout", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ positions }),
  });
  if (!r.ok) throw new Error(`/api/network/layout -> ${r.status}`);
  return r.json();
}

async function resetLayoutApi() {
  const r = await fetch("/api/network/layout/reset", { method: "POST" });
  if (!r.ok) throw new Error(`/api/network/layout/reset -> ${r.status}`);
  return r.json();
}

// Packet animation duration (ms) — from flow.push event-ts to flow.ack.
// We also cap at this duration if no ack arrives (unlikely, but keeps
// the canvas from accumulating infinite dots if events are dropped).
const PACKET_DURATION_MS = 2400;
const COLLAPSE_ZOOM = 0.55;  // below this, group children collapse into super-nodes.

export function CanvasView({ onSelect, journeyId, journeyBus }) {
  // --- data --------------------------------------------------------------
  const [network, setNetwork] = useState(null);
  const [packets, setPackets] = useState({ by_executor: {}, in_flight: [] });
  const [error, setError] = useState(null);

  // --- view state --------------------------------------------------------
  const [transform, setTransform] = useState({ x: 0, y: 0, k: 0.9 });
  const [selected, setSelected] = useState(null);   // { kind, id }
  const [paused, setPaused] = useState(false);
  const [hoverId, setHoverId] = useState(null);

  // Active animated packets (keyed — so SSE deltas don't rebuild them).
  const [packetDots, setPacketDots] = useState([]);   // [{key, edgeKey, startedAt, from, to, node}]
  const animationTick = useRef(0);
  const [tickFrame, setTickFrame] = useState(0);      // re-render pulse while animating

  // Journey overlay — visited path for a selected work item.
  const [journey, setJourney] = useState(null);

  const svgRef = useRef(null);
  const dragRef = useRef(null);

  // Initial load.
  useEffect(() => {
    let cancel = false;
    fetchNetwork().then((n) => { if (!cancel) setNetwork(n); })
                  .catch((e) => { if (!cancel) setError(String(e)); });
    fetchPackets().then((p) => { if (!cancel) setPackets(p); })
                  .catch(() => { /* non-fatal */ });
    return () => { cancel = true; };
  }, []);

  // Subscribe to SSE flow events. The shared /api/events stream already
  // tails events.jsonl; we filter for flow.* kinds here and use them to
  // spawn animated packet dots + nudge packet-state refreshes.
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
          if (!to || !node) return;
          if (from) {
            // Only animate when there's a real source → target edge to ride.
            setPacketDots((prev) => {
              const key = `${node}-${data.ts}-${from}-${to}`;
              return [...prev, {
                key, edgeKey: `${from}|${to}`,
                startedAt: performance.now(),
                node, from, to,
              }];
            });
          }
        }
        // Refresh packet state on any flow event (cheap on small projects).
        fetchPackets().then(setPackets).catch(() => {});
      } catch { /* ignore */ }
    };
    return () => es.close();
  }, [paused]);

  // RAF loop — drives packet-dot animation and sweeps expired dots.
  useEffect(() => {
    if (paused) return;
    let rafId;
    const tick = () => {
      const now = performance.now();
      animationTick.current = now;
      setPacketDots((prev) => {
        if (prev.length === 0) return prev;
        return prev.filter((p) => now - p.startedAt < PACKET_DURATION_MS);
      });
      setTickFrame((n) => (n + 1) % 1000000);
      rafId = requestAnimationFrame(tick);
    };
    rafId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafId);
  }, [paused]);

  // Listen for item-journey requests from the rest of the app (Ready,
  // Backlog, History tabs fire a CustomEvent — see app.js glue).
  useEffect(() => {
    if (!journeyId) { setJourney(null); return; }
    let cancel = false;
    fetchJourney(journeyId).then((j) => { if (!cancel) setJourney(j); })
                           .catch(() => { if (!cancel) setJourney(null); });
    return () => { cancel = true; };
  }, [journeyId]);

  // --- layout computation ------------------------------------------------
  const layout = useMemo(() => {
    if (!network) return null;
    return buildLayout(network.executors, network.routes,
      (network.layout && network.layout.positions) || {});
  }, [network]);

  // Build edge-key -> edge map for packet lookup.
  const edgeByKey = useMemo(() => {
    if (!layout) return new Map();
    const m = new Map();
    for (const e of layout.edges) {
      m.set(`${e.from}|${e.to}`, e);
    }
    return m;
  }, [layout]);

  // --- activity / saturation --------------------------------------------
  // Saturation scales with inbox depth + active count, normalised by max.
  const activityByExec = useMemo(() => {
    const out = {};
    let peak = 1;
    for (const [eid, slot] of Object.entries(packets.by_executor || {})) {
      const v = (slot.inbox_depth || 0) + (slot.active_depth || 0);
      if (v > peak) peak = v;
      out[eid] = v;
    }
    const sat = {};
    for (const [eid, v] of Object.entries(out)) {
      sat[eid] = v === 0 ? 0.2 : Math.min(1, 0.35 + 0.65 * (v / peak));
    }
    return { sat, raw: out };
  }, [packets]);

  // Journey highlight: edge set + node set.
  const journeyEdges = useMemo(() => {
    if (!journey || !journey.visited || journey.visited.length < 2) return new Set();
    const s = new Set();
    for (let i = 0; i < journey.visited.length - 1; i++) {
      s.add(`${journey.visited[i]}|${journey.visited[i + 1]}`);
    }
    return s;
  }, [journey]);

  const journeyNodes = useMemo(() => new Set(journey?.visited || []), [journey]);

  // --- pan / zoom handlers ----------------------------------------------
  const handleWheel = useCallback((e) => {
    e.preventDefault();
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const cx = e.clientX - rect.left;
    const cy = e.clientY - rect.top;
    const scale = e.deltaY < 0 ? 1.1 : 1 / 1.1;
    setTransform((t) => {
      const nk = Math.min(3, Math.max(0.15, t.k * scale));
      const actual = nk / t.k;
      return {
        k: nk,
        x: cx - (cx - t.x) * actual,
        y: cy - (cy - t.y) * actual,
      };
    });
  }, []);

  const panRef = useRef(null);
  const onSvgPointerDown = useCallback((e) => {
    // Only pan when the user grabs background (not a node).
    if (e.target.closest && e.target.closest(".fx-node")) return;
    panRef.current = {
      startX: e.clientX, startY: e.clientY,
      base: { ...transform },
      pointerId: e.pointerId,
    };
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* ignore */ }
  }, [transform]);

  const onSvgPointerMove = useCallback((e) => {
    const p = panRef.current;
    if (p && p.pointerId === e.pointerId) {
      setTransform({
        k: p.base.k,
        x: p.base.x + (e.clientX - p.startX),
        y: p.base.y + (e.clientY - p.startY),
      });
      return;
    }
    const d = dragRef.current;
    if (d && d.pointerId === e.pointerId && layout) {
      const dx = (e.clientX - d.startX) / transform.k;
      const dy = (e.clientY - d.startY) / transform.k;
      // Update layout positions in-place; we save on pointer-up.
      const pos = layout.positions[d.id];
      if (pos) {
        pos.x = d.baseX + dx;
        pos.y = d.baseY + dy;
        setTickFrame((n) => n + 1);
      }
    }
  }, [transform, layout]);

  const onSvgPointerUp = useCallback((e) => {
    const p = panRef.current;
    if (p && p.pointerId === e.pointerId) {
      panRef.current = null;
      try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
    }
    const d = dragRef.current;
    if (d && d.pointerId === e.pointerId) {
      dragRef.current = null;
      if (layout && layout.positions[d.id]) {
        const pos = layout.positions[d.id];
        postLayout({ [d.id]: { x: pos.x, y: pos.y } }).catch(() => {});
      }
    }
  }, [layout]);

  // Keyboard: +/- zoom, 0 reset, tab cycle, arrows pan.
  useEffect(() => {
    const onKey = (e) => {
      // Only when canvas has focus (its container) — guard against
      // swallowing text-input keys elsewhere.
      const host = svgRef.current?.closest(".fx-canvas");
      if (!host || !host.contains(document.activeElement) && document.activeElement !== document.body) {
        // Still allow global +/- if focus is body.
        if (document.activeElement !== document.body) return;
      }
      if (e.key === "+" || e.key === "=") {
        setTransform((t) => ({ ...t, k: Math.min(3, t.k * 1.15) }));
      } else if (e.key === "-") {
        setTransform((t) => ({ ...t, k: Math.max(0.15, t.k / 1.15) }));
      } else if (e.key === "0") {
        setTransform({ x: 40, y: 40, k: 0.9 });
      } else if (e.key === "ArrowLeft") {
        setTransform((t) => ({ ...t, x: t.x + 40 }));
      } else if (e.key === "ArrowRight") {
        setTransform((t) => ({ ...t, x: t.x - 40 }));
      } else if (e.key === "ArrowUp") {
        setTransform((t) => ({ ...t, y: t.y + 40 }));
      } else if (e.key === "ArrowDown") {
        setTransform((t) => ({ ...t, y: t.y - 40 }));
      } else if (e.key === " ") {
        setPaused((p) => !p);
        e.preventDefault();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // --- drag a node -------------------------------------------------------
  const onNodePointerDown = useCallback((e, id) => {
    if (!layout) return;
    const pos = layout.positions[id];
    if (!pos) return;
    e.stopPropagation();
    dragRef.current = {
      id, pointerId: e.pointerId,
      startX: e.clientX, startY: e.clientY,
      baseX: pos.x, baseY: pos.y,
    };
    try { e.currentTarget.ownerSVGElement.setPointerCapture(e.pointerId); } catch { /* ignore */ }
  }, [layout]);

  // --- selection handlers -----------------------------------------------
  const selectNode = (id) => {
    setSelected({ kind: "executor", id });
    // Surface executor details in the right-dock using the existing panel.
    // The existing panel keys off work-item id via onSelect; we fall back
    // to showing inline detail here when the id isn't a work item.
    // For executors the user also sees inline panel (below).
  };

  const selectEdge = (edge) => {
    setSelected({ kind: "edge", id: `${edge.from}|${edge.to}|${edge.route?.condition || ""}`,
                  edge });
  };

  // Reset layout.
  const onResetLayout = async () => {
    try {
      await resetLayoutApi();
      const n = await fetchNetwork();
      setNetwork(n);
    } catch (e) { /* ignore */ }
  };

  // --- early-returns -----------------------------------------------------
  if (error) return h("div", { class: "empty", style: "color: var(--err)" },
    `Canvas error: ${error}`);
  if (!network) return h("div", { class: "muted" }, "Loading flow network…");
  if (!network.executors || network.executors.length === 0) {
    return h("div", { class: "empty" },
      "No flow network yet. Run `hopewell network init && hopewell network defaults bootstrap`.");
  }
  if (!layout) return h("div", { class: "muted" }, "Computing layout…");

  // Collapse child executors of groups when zoomed out.
  const collapsedChildren = new Set();
  if (transform.k < COLLAPSE_ZOOM) {
    for (const ex of network.executors) {
      if (ex.parent) collapsedChildren.add(ex.id);
    }
  }

  // Visible view-box in world space (for the minimap viewport rect).
  const svgEl = svgRef.current;
  const cvWidth = svgEl ? svgEl.clientWidth : 1000;
  const cvHeight = svgEl ? svgEl.clientHeight : 600;
  const viewBox = {
    x: (-transform.x) / transform.k,
    y: (-transform.y) / transform.k,
    w: cvWidth / transform.k,
    h: cvHeight / transform.k,
  };

  // --- render packets (dots) --------------------------------------------
  const now = animationTick.current || performance.now();
  const renderedDots = packetDots.map((p) => {
    const edge = edgeByKey.get(p.edgeKey);
    if (!edge) return null;
    const t = Math.min(1, (now - p.startedAt) / PACKET_DURATION_MS);
    const pt = pointAtT(edge.points, t);
    // Color by work-item id hash for distinguishability.
    let hh = 0;
    for (let i = 0; i < p.node.length; i++) hh = (hh * 31 + p.node.charCodeAt(i)) >>> 0;
    const color = `hsl(${hh % 360}, 70%, 65%)`;
    return h("g", { key: p.key, class: "fx-packet" },
      h("circle", { cx: pt.x, cy: pt.y, r: 5.5,
        fill: color, stroke: "#14161a", "stroke-width": 1.5 }),
      h("text", { class: "fx-packet-label", x: pt.x, y: pt.y - 10,
        "text-anchor": "middle", fill: color }, p.node),
    );
  });

  // Detail-panel contents for executors/edges (inline, embedded in canvas).
  const detailPanel = renderInlineDetail({
    selected, network, packets, onSelect,
    onJumpItem: (id) => { onSelect && onSelect(id); },
  });

  return h("div", { class: "fx-canvas", tabIndex: 0 },
    // Toolbar.
    h("div", { class: "fx-toolbar" },
      h("button", { onClick: () => setPaused((p) => !p),
        title: "Pause/resume packet animation (space)" },
        paused ? "▶ resume" : "⏸ pause"),
      h("button", { onClick: () => setTransform({ x: 40, y: 40, k: 0.9 }),
        title: "Reset view (0)" }, "reset view"),
      h("button", { onClick: onResetLayout,
        title: "Discard manual drags and re-run dagre" }, "reset layout"),
      h("span", { class: "fx-toolbar-hint muted" },
        `zoom ${Math.round(transform.k * 100)}%`),
      journeyId && h("span", { class: "fx-toolbar-journey" },
        `journey: ${journeyId}`,
        h("button", { class: "fx-toolbar-clear",
          onClick: () => journeyBus && journeyBus(null) }, "×")),
    ),

    // Main SVG stage.
    h("svg", {
      ref: svgRef, class: "fx-stage",
      onWheel: handleWheel,
      onPointerDown: onSvgPointerDown,
      onPointerMove: onSvgPointerMove,
      onPointerUp: onSvgPointerUp,
      onPointerCancel: onSvgPointerUp,
    },
      // Marker definitions.
      h("defs", null,
        h("marker", {
          id: "fx-arrow", viewBox: "0 -5 10 10", refX: 8, refY: 0,
          markerWidth: 6, markerHeight: 6, orient: "auto" },
          h("path", { d: "M0,-5 L10,0 L0,5 Z", fill: "#6a7384" })),
        h("marker", {
          id: "fx-arrow-bold", viewBox: "0 -5 10 10", refX: 8, refY: 0,
          markerWidth: 7, markerHeight: 7, orient: "auto" },
          h("path", { d: "M0,-5 L10,0 L0,5 Z", fill: "#c4cbd6" })),
        h("marker", {
          id: "fx-arrow-hl", viewBox: "0 -5 10 10", refX: 8, refY: 0,
          markerWidth: 8, markerHeight: 8, orient: "auto" },
          h("path", { d: "M0,-5 L10,0 L0,5 Z", fill: "#f5b556" })),
      ),

      h("g", {
        class: "fx-world",
        transform: `translate(${transform.x}, ${transform.y}) scale(${transform.k})`,
      },
        // Edges behind nodes.
        layout.edges.map((edge) => {
          const key = `${edge.from}|${edge.to}|${edge.route?.condition || ""}`;
          const hl = journeyEdges.has(`${edge.from}|${edge.to}`);
          return h(EdgePath, {
            key, edge, highlighted: hl,
            onClick: (e) => { e.stopPropagation(); selectEdge(edge); },
          });
        }),

        // Packet dots.
        renderedDots,

        // Nodes.
        network.executors.map((ex) => {
          if (collapsedChildren.has(ex.id)) return null;
          const pos = layout.positions[ex.id];
          if (!pos) return null;
          const kind = dominantKind(ex.components);
          const sat = activityByExec.sat[ex.id] || 0.2;
          const slot = packets.by_executor[ex.id] || {};
          const inJourney = journeyNodes.has(ex.id);
          const sel = selected && selected.kind === "executor" && selected.id === ex.id;
          return h(NodeShape, {
            key: ex.id,
            pos: { x: pos.x, y: pos.y, width: pos.width, height: pos.height },
            executor: ex,
            kind,
            selected: sel || inJourney,
            saturation: sat,
            depth: slot.inbox_depth || 0,
            active: slot.active_depth || 0,
            onClick: (e) => { e.stopPropagation(); selectNode(ex.id); },
            onPointerDown: (e) => onNodePointerDown(e, ex.id),
          });
        }),
      ),
    ),

    // Minimap (bottom-right).
    h("div", { class: "fx-minimap-wrap" },
      h(Minimap, {
        layout, viewBox,
        executors: network.executors,
        onJumpTo: (wx, wy) => {
          // Centre the canvas on (wx, wy).
          const svg = svgRef.current;
          if (!svg) return;
          const W = svg.clientWidth, H = svg.clientHeight;
          setTransform((t) => ({ k: t.k, x: W / 2 - wx * t.k, y: H / 2 - wy * t.k }));
        },
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
        "edges: required=bold · optional=thin · conditional=pill · forbidden=red ⊘"),
    ),

    // Inline detail panel (right-dock). Stays open across selections.
    detailPanel,
  );
}

// ---------------------------------------------------------------------------
// Inline detail panel (executor / edge / packet detail)
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
          ? h("div", { class: "muted" }, "— empty —")
          : h("ul", { class: "fx-detail-list" },
              slot.inbox.map((p, i) => h("li", { key: i },
                h("a", { href: "#",
                  onClick: (ev) => { ev.preventDefault(); onJumpItem(p.node); } },
                  p.node),
                " ", h("span", { class: "muted" }, p.from_executor ? `from ${p.from_executor}` : "(external)"),
              ))),
      ),
      h("div", { class: "fx-detail-section" },
        h("div", { class: "fx-detail-heading" },
          `active at executor (${slot.active_depth || 0})`),
        (slot.active || []).length === 0
          ? h("div", { class: "muted" }, "— empty —")
          : h("ul", { class: "fx-detail-list" },
              slot.active.map((a, i) => h("li", { key: i },
                h("a", { href: "#",
                  onClick: (ev) => { ev.preventDefault(); onJumpItem(a.node_id); } },
                  a.node_id),
                " ", h("span", null, a.title || ""),
              ))),
      ),
      Object.keys(ex.component_data || {}).length > 0 && h("div", { class: "fx-detail-section" },
        h("div", { class: "fx-detail-heading" }, "component_data"),
        h("pre", { class: "fx-detail-code" },
          JSON.stringify(ex.component_data, null, 2)),
      ),
    );
  }
  if (selected.kind === "edge") {
    const r = selected.edge?.route || {};
    return h("aside", { class: "fx-detail" },
      h("div", { class: "fx-detail-head" },
        h("span", { class: "fx-detail-kind" }, "route"),
      ),
      h("div", { class: "fx-detail-label" },
        selected.edge?.from, " → ", selected.edge?.to),
      h("div", { class: "fx-detail-section" },
        h("dl", { class: "fx-kv" },
          h("dt", null, "required"), h("dd", null, r.required ? "yes" : "no"),
          h("dt", null, "condition"), h("dd", null, r.condition || "—"),
          h("dt", null, "label"), h("dd", null, r.label || "—"),
          h("dt", null, "created"), h("dd", null, r.created || "—"),
        ),
      ),
    );
  }
  return null;
}
