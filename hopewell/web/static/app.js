// Hopewell web UI — Preact + D3, zero build.
//
// Pulls state from /api/state once + subscribes to /api/events (SSE)
// for incremental refreshes. Four views share one state atom; the
// detail panel lazily fetches /api/node/<id> on demand.

import { h, render, Fragment } from "https://esm.sh/preact@10.22.0";
import { useState, useEffect, useMemo, useRef, useCallback }
  from "https://esm.sh/preact@10.22.0/hooks";
import * as d3 from "https://esm.sh/d3@7.9.0";

// ---------------------------------------------------------------------------
// State + API
// ---------------------------------------------------------------------------

const EMPTY_STATE = {
  project: { name: "", root: "" },
  systems: [],
  nodes: [],
  edges: [],
  uat: [],
  claims: [],
  waves: { waves: [], critical_path: [], depth: 0, max_width: 0 },
};

async function fetchState() {
  const r = await fetch("/api/state");
  if (!r.ok) throw new Error(`/api/state -> ${r.status}`);
  return r.json();
}

async function fetchNode(id) {
  const r = await fetch(`/api/node/${encodeURIComponent(id)}`);
  if (!r.ok) throw new Error(`/api/node -> ${r.status}`);
  return r.json();
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}

// Dominant-component colour. Stable hash so the same component always
// gets the same hue; tweak MEANINGFUL ones explicitly.
const COMPONENT_COLOUR_OVERRIDES = {
  "work-item":          "#6ea8ff",
  "deliverable":        "#5bd49b",
  "user-facing":        "#a17aff",
  "defect":             "#ff6b6b",
  "documentation":      "#8a93a2",
  "grouping":           "#f5b556",
  "test":               "#7fd4d4",
  "design":             "#e87ed4",
  "needs-uat":          "#f5b556",
};

function colourFor(component) {
  if (COMPONENT_COLOUR_OVERRIDES[component]) return COMPONENT_COLOUR_OVERRIDES[component];
  // deterministic hash -> hsl
  let h = 0;
  for (let i = 0; i < component.length; i++) h = (h * 31 + component.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360}, 55%, 62%)`;
}

// Pick the "dominant" component for a node — first one we have a colour
// for, else first component at all, else "node".
function dominantComponent(n) {
  const pref = ["defect", "user-facing", "deliverable", "grouping",
                "test", "design", "documentation", "work-item"];
  for (const p of pref) if (n.components && n.components.includes(p)) return p;
  return (n.components && n.components[0]) || "node";
}

// ---------------------------------------------------------------------------
// Root component
// ---------------------------------------------------------------------------

function App() {
  const [state, setState] = useState(EMPTY_STATE);
  const [tab, setTab] = useState("tree");
  const [detailId, setDetailId] = useState(null);
  const [sseOk, setSseOk] = useState(false);
  const [lastEvent, setLastEvent] = useState("no events yet");
  const [error, setError] = useState(null);

  const reload = useCallback(async () => {
    try {
      const s = await fetchState();
      setState(s);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => { reload(); }, [reload]);

  // SSE connection.
  useEffect(() => {
    const es = new EventSource("/api/events");
    es.onopen = () => setSseOk(true);
    es.onerror = () => setSseOk(false);
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        const kind = data.kind || "event";
        const who = data.actor ? `@${data.actor.replace(/^@/, "")} ` : "";
        const where = data.node ? `on ${data.node}` : "";
        setLastEvent(`${data.ts || ""} ${kind} ${who}${where}`.trim());
        // Any graph-mutating event → resync. Cheap; small projects.
        reload();
      } catch { /* ignore */ }
    };
    return () => es.close();
  }, [reload]);

  // Project name in header.
  useEffect(() => {
    const el = document.getElementById("project-name");
    if (el) el.textContent = state.project.name ? ` — ${state.project.name}` : "";
    const m = document.getElementById("metric-total");
    if (m) m.textContent = `${state.nodes.length} node${state.nodes.length === 1 ? "" : "s"}`;
    const d = document.getElementById("status-dot");
    if (d) {
      d.classList.toggle("connected", sseOk);
      d.title = sseOk ? "SSE connected" : "SSE disconnected";
    }
    const le = document.getElementById("last-event");
    if (le) le.textContent = lastEvent;
  }, [state, sseOk, lastEvent]);

  // Tab buttons live outside the Preact root — rebind on mount.
  useEffect(() => {
    const tabs = document.querySelectorAll("nav.tabs .tab");
    const handler = (e) => {
      const t = e.currentTarget.getAttribute("data-tab");
      setTab(t);
      tabs.forEach((x) => x.classList.toggle("active", x === e.currentTarget));
    };
    tabs.forEach((b) => b.addEventListener("click", handler));
    const refresh = document.getElementById("refresh");
    if (refresh) refresh.addEventListener("click", reload);
    return () => {
      tabs.forEach((b) => b.removeEventListener("click", handler));
      if (refresh) refresh.removeEventListener("click", reload);
    };
  }, [reload]);

  const onSelect = useCallback((id) => setDetailId(id), []);

  return h(Fragment, null,
    error && h("div", { class: "empty" }, `Error loading state: ${error}`),
    tab === "tree"     && h(TreeView,     { state, onSelect }),
    tab === "canvas"   && h(CanvasView,   { state, onSelect }),
    tab === "timeline" && h(TimelineView, { state, onSelect }),
    tab === "uat"      && h(UatView,      { state, onSelect, reload }),
    detailId && h(Detail, {
      id: detailId,
      onClose: () => setDetailId(null),
    }),
  );
}

// ---------------------------------------------------------------------------
// Tree view
// ---------------------------------------------------------------------------

function TreeView({ state, onSelect }) {
  const [collapsed, setCollapsed] = useState(() => new Set());

  const byId = useMemo(() => {
    const m = new Map();
    for (const n of state.nodes) m.set(n.id, { ...n, children: [] });
    return m;
  }, [state.nodes]);

  const roots = useMemo(() => {
    const rs = [];
    for (const n of byId.values()) {
      if (n.parent && byId.has(n.parent)) byId.get(n.parent).children.push(n);
      else rs.push(n);
    }
    // Stable ordering: by id.
    const sortRec = (n) => { n.children.sort((a, b) => a.id.localeCompare(b.id)); n.children.forEach(sortRec); };
    rs.sort((a, b) => a.id.localeCompare(b.id));
    rs.forEach(sortRec);
    return rs;
  }, [byId]);

  const toggle = (id) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  if (state.nodes.length === 0) {
    return h("div", { class: "empty" }, "No nodes. Create some with `hopewell new`.");
  }

  const renderNode = (n) => {
    const isLeaf = n.children.length === 0;
    const isCollapsed = collapsed.has(n.id);
    return h("li", { key: n.id },
      h("div", { class: "row" },
        h("span", {
          class: "caret" + (isLeaf ? " leaf" : ""),
          onClick: () => !isLeaf && toggle(n.id),
        }, isLeaf ? "" : (isCollapsed ? "▸" : "▾")),
        h("span", { class: "id", onClick: () => onSelect(n.id) }, n.id),
        h("span", { class: "title", onClick: () => onSelect(n.id) }, " " + n.title),
        h("span", { class: `badge status-${n.status}` }, n.status),
        n.priority && h("span", { class: "badge" }, n.priority),
      ),
      !isLeaf && !isCollapsed && h("ul", null, n.children.map(renderNode)),
    );
  };

  return h("div", { class: "tree" },
    h("ul", null, roots.map(renderNode)),
  );
}

// ---------------------------------------------------------------------------
// Canvas view — D3 force graph
// ---------------------------------------------------------------------------

function CanvasView({ state, onSelect }) {
  const svgRef = useRef(null);
  const simRef = useRef(null);

  useEffect(() => {
    const svgEl = svgRef.current;
    if (!svgEl) return;
    const rect = svgEl.getBoundingClientRect();
    const width = rect.width || 800;
    const height = rect.height || 600;

    const nodes = state.nodes.map((n) => ({
      id: n.id,
      title: n.title,
      status: n.status,
      dominant: dominantComponent(n),
      components: n.components || [],
    }));
    const nodeIds = new Set(nodes.map((n) => n.id));
    const links = state.edges
      .filter((e) => nodeIds.has(e.from) && nodeIds.has(e.to))
      .map((e) => ({ source: e.from, target: e.to, kind: e.kind }));

    const svg = d3.select(svgEl);
    svg.selectAll("*").remove();

    const g = svg.append("g");

    // Zoom.
    svg.call(d3.zoom()
      .scaleExtent([0.25, 4])
      .on("zoom", (ev) => g.attr("transform", ev.transform)));

    const link = g.append("g")
      .selectAll("line")
      .data(links)
      .join("line")
      .attr("class", (d) => `edge ${d.kind}`);

    const node = g.append("g")
      .selectAll("g")
      .data(nodes)
      .join("g")
      .attr("class", "node")
      .call(d3.drag()
        .on("start", (ev, d) => {
          if (!ev.active) simRef.current.alphaTarget(0.3).restart();
          d.fx = d.x; d.fy = d.y;
        })
        .on("drag", (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
        .on("end", (ev, d) => {
          if (!ev.active) simRef.current.alphaTarget(0);
          d.fx = null; d.fy = null;
        }));

    node.append("circle")
      .attr("r", 9)
      .attr("fill", (d) => colourFor(d.dominant))
      .attr("opacity", (d) => ["done", "archived", "cancelled"].includes(d.status) ? 0.45 : 0.95)
      .on("click", (_, d) => onSelect(d.id));

    node.append("text")
      .attr("x", 12)
      .attr("y", 4)
      .text((d) => d.id);

    node.append("title")
      .text((d) => `${d.id}  ${d.title}\nstatus: ${d.status}\ncomponents: ${d.components.join(", ")}`);

    const sim = d3.forceSimulation(nodes)
      .force("link", d3.forceLink(links).id((d) => d.id).distance(65).strength(0.4))
      .force("charge", d3.forceManyBody().strength(-180))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force("collide", d3.forceCollide(14))
      .on("tick", () => {
        link
          .attr("x1", (d) => d.source.x).attr("y1", (d) => d.source.y)
          .attr("x2", (d) => d.target.x).attr("y2", (d) => d.target.y);
        node.attr("transform", (d) => `translate(${d.x},${d.y})`);
      });
    simRef.current = sim;

    return () => sim.stop();
  }, [state.nodes, state.edges, onSelect]);

  // Legend: dominant-components present.
  const legendComponents = useMemo(() => {
    const set = new Set(state.nodes.map(dominantComponent));
    return Array.from(set).sort();
  }, [state.nodes]);

  if (state.nodes.length === 0) {
    return h("div", { class: "empty" }, "No nodes to plot.");
  }

  return h("div", { class: "canvas-wrap" },
    h("svg", { ref: svgRef }),
    h("div", { class: "legend" },
      h("div", { style: "font-weight:600; margin-bottom:4px" }, "Dominant component"),
      legendComponents.map((c) => h("div", { key: c },
        h("span", { class: "sw", style: `background:${colourFor(c)}` }),
        c,
      )),
      h("div", { style: "margin-top:6px; color: var(--fg-muted); font-size:10px" },
        "Edges: blocks=red, parent=dashed, consumes=blue, related=violet"),
    ),
  );
}

// ---------------------------------------------------------------------------
// Timeline view — wave schedule
// ---------------------------------------------------------------------------

function TimelineView({ state, onSelect }) {
  const { waves } = state.waves || { waves: [] };
  const critical = new Set(state.waves?.critical_path || []);
  if (!waves || waves.length === 0) {
    return h("div", { class: "empty" }, "No schedulable nodes right now.");
  }
  return h("div", { class: "timeline" },
    h("div", { class: "muted", style: "margin-bottom:6px" },
      `${waves.length} wave${waves.length === 1 ? "" : "s"}`
      + `, depth ${state.waves.depth}`
      + `, max width ${state.waves.max_width}`
      + `, critical path: ${state.waves.critical_path.join(" → ") || "(empty)"}`),
    waves.map((w) => h("div", { class: "wave", key: w.n },
      h("h3", null, `Wave ${w.n}  (${w.nodes.length})`),
      h("div", { class: "chips" },
        w.nodes.map((id) => h("span", {
          key: id,
          class: "chip" + (critical.has(id) ? " on-critical" : ""),
          onClick: () => onSelect(id),
        }, id)),
      ),
    )),
  );
}

// ---------------------------------------------------------------------------
// UAT view
// ---------------------------------------------------------------------------

function UatView({ state, onSelect, reload }) {
  const [busy, setBusy] = useState(null);
  const [err, setErr] = useState(null);

  const act = async (id, verb) => {
    setBusy(`${id}:${verb}`);
    setErr(null);
    try {
      await postJSON(`/api/node/${encodeURIComponent(id)}/uat-${verb}`, {});
      await reload();
    } catch (e) {
      setErr(`${verb} ${id} failed: ${e}`);
    } finally {
      setBusy(null);
    }
  };

  if (!state.uat || state.uat.length === 0) {
    return h("div", { class: "empty" }, "No UAT-tracked nodes. Flag some with `hopewell uat flag`.");
  }

  return h("div", { class: "uat" },
    err && h("div", { class: "empty", style: "color: var(--err)" }, err),
    state.uat.map((u) => h("div", { class: "item", key: u.id },
      h("div", null,
        h("div", { class: "id", style: "cursor:pointer", onClick: () => onSelect(u.id) }, u.id),
        h("span", { class: `status-chip ${u.uat_status}` }, u.uat_status),
      ),
      h("div", null,
        h("div", null, u.title,
          " ", h("span", { class: "muted" }, `(${u.node_status}${u.owner ? `, ${u.owner}` : ""})`)),
        u.acceptance_criteria && u.acceptance_criteria.length > 0 && h("ul", { class: "criteria" },
          u.acceptance_criteria.map((c, i) => h("li", { key: i }, c))),
        u.notes && h("div", { class: "muted", style: "font-size:11px; margin-top:4px" },
          `note: ${u.notes}`),
        u.failure_reason && h("div", { style: "color: var(--err); font-size:11px; margin-top:4px" },
          `fail reason: ${u.failure_reason}`),
      ),
      h("div", { class: "actions" },
        h("button", { class: "pass",  disabled: busy, onClick: () => act(u.id, "pass") }, "pass"),
        h("button", { class: "fail",  disabled: busy, onClick: () => act(u.id, "fail") }, "fail"),
        h("button", { class: "waive", disabled: busy, onClick: () => act(u.id, "waive") }, "waive"),
      ),
    )),
  );
}

// ---------------------------------------------------------------------------
// Detail side panel
// ---------------------------------------------------------------------------

function Detail({ id, onClose }) {
  const [node, setNode] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let cancel = false;
    setNode(null);
    setErr(null);
    fetchNode(id).then((n) => { if (!cancel) setNode(n); })
                 .catch((e) => { if (!cancel) setErr(String(e)); });
    return () => { cancel = true; };
  }, [id]);

  useEffect(() => {
    const el = document.getElementById("detail");
    if (!el) return;
    el.classList.remove("hidden");
    const close = document.getElementById("detail-close");
    if (close) close.onclick = onClose;
    const onKey = (ev) => { if (ev.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => {
      el.classList.add("hidden");
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const body = document.getElementById("detail-body");
  if (!body) return null;

  let content;
  if (err) {
    content = h("div", { class: "empty", style: "color: var(--err)" }, err);
  } else if (!node) {
    content = h("div", { class: "muted" }, `Loading ${id}…`);
  } else {
    content = h(Fragment, null,
      h("h2", null, `${node.id} — ${node.title}`),
      h("dl", { class: "kv" },
        h("dt", null, "status"),    h("dd", null, node.status),
        h("dt", null, "priority"),  h("dd", null, node.priority),
        h("dt", null, "owner"),     h("dd", null, node.owner || "—"),
        h("dt", null, "parent"),    h("dd", null, node.parent || "—"),
        h("dt", null, "created"),   h("dd", null, node.created),
        h("dt", null, "updated"),   h("dd", null, node.updated),
        h("dt", null, "components"), h("dd", null, (node.components || []).join(", ")),
        h("dt", null, "blocks"),    h("dd", null, (node.blocks || []).join(", ") || "—"),
        h("dt", null, "blocked_by"), h("dd", null, (node.blocked_by || []).join(", ") || "—"),
      ),
      node.body && h(Fragment, null,
        h("h3", null, "body"),
        h("pre", null, node.body)),
      node.notes && node.notes.length > 0 && h(Fragment, null,
        h("h3", null, `notes (${node.notes.length})`),
        h("pre", null, node.notes.join("\n"))),
      node.component_data && Object.keys(node.component_data).length > 0 && h(Fragment, null,
        h("h3", null, "component_data"),
        h("pre", null, JSON.stringify(node.component_data, null, 2))),
    );
  }

  render(content, body);
  return null;
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

render(h(App), document.getElementById("app"));
