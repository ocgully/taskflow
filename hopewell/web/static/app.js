// Hopewell web UI — Preact + D3, zero build.
//
// Pulls state from /api/state once + subscribes to /api/events (SSE)
// for incremental refreshes. Four views share one state atom; the
// detail panel lazily fetches /api/node/<id> on demand.

import { h, render, Fragment } from "https://esm.sh/preact@10.22.0";
import { useState, useEffect, useMemo, useRef, useCallback }
  from "https://esm.sh/preact@10.22.0/hooks";
import { marked } from "https://esm.sh/marked@12.0.2";
import { CanvasView } from "/static/canvas.js";
import { DocViewer } from "/static/viewer.js";

// ---------------------------------------------------------------------------
// URL-hash route helpers (HW-0032 viewer deep-link: #/doc/<NODE_ID>)
// ---------------------------------------------------------------------------

const DOC_HASH_RE = /^#\/doc\/([^/?#]+)/;

function parseDocHash(hashStr) {
  const m = (hashStr || "").match(DOC_HASH_RE);
  return m ? decodeURIComponent(m[1]) : null;
}

function setDocHash(nodeId) {
  const want = nodeId ? `#/doc/${encodeURIComponent(nodeId)}` : "#";
  if (location.hash !== want) {
    // replaceState avoids polluting history during rapid nav; leave
    // back-button support for user-initiated open/close.
    if (nodeId) location.hash = want;
    else history.replaceState(null, "", location.pathname + location.search + "#");
  }
}

// Markdown renderer config: GFM on, line breaks on, no raw HTML passthrough.
// marked escapes HTML by default when `mangle`/`headerIds` aren't configured;
// we additionally set `breaks: true` so notes-style newlines render as <br>.
marked.setOptions({ gfm: true, breaks: true });

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
  const [tab, setTab] = useState("canvas");     // HW-0029: canvas is the prominent default.
  const [detailId, setDetailId] = useState(null);
  const [journeyId, setJourneyId] = useState(null);   // HW-0029: item-journey overlay
  const [viewerId, setViewerId] = useState(null);    // HW-0032: md viewer modal
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

  // onSelect routes clicks from any view:
  //   * Always open the detail panel for the node.
  //   * Additionally arm a journey-overlay so the Canvas tab highlights
  //     the work item's path through the flow network (HW-0029 criterion
  //     "clicking a work item anywhere in the UI highlights its path on
  //     the canvas as a colored trail").
  // HW-0032: clicking a node ID opens the markdown doc viewer as a modal
  // (with #/doc/<id> hash for deep-linking) AND arms the journey overlay.
  // The Detail sidebar (old UX) is reachable from the viewer's "sidebar
  // detail" link, or programmatically via onOpenDetail; keeping both so
  // features like UAT actions in the sidebar remain accessible.
  const onSelect = useCallback((id) => {
    setJourneyId(id);
    setDetailId(id);     // keep Detail sidebar in sync (UAT actions live there)
    setViewerId(id);
    setDocHash(id);
  }, []);

  // HW-0032 — hash route: #/doc/<NODE_ID> opens the markdown viewer modal.
  // Initial load + every hashchange syncs viewerId to the hash.
  useEffect(() => {
    const sync = () => setViewerId(parseDocHash(location.hash));
    sync();
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  const openViewer = useCallback((id) => {
    setViewerId(id);
    setDocHash(id);
  }, []);

  const closeViewer = useCallback(() => {
    setViewerId(null);
    setDocHash(null);
  }, []);

  return h(Fragment, null,
    error && h("div", { class: "empty" }, `Error loading state: ${error}`),
    tab === "backlog"  && h(BacklogView,  { state, onSelect }),
    tab === "canvas"   && h(CanvasView,   {
      onSelect,
      journeyId,
      journeyBus: setJourneyId,
    }),
    tab === "timeline" && h(TimelineView, { state, onSelect }),
    tab === "uat"      && h(UatView,      { state, onSelect, reload }),
    tab === "history"  && h(HistoryView,  { onSelect }),
    detailId && h(Detail, {
      id: detailId,
      onSelect,
      onOpenViewer: openViewer,
      onClose: () => setDetailId(null),
    }),
    viewerId && h(DocViewer, {
      nodeId: viewerId,
      onClose: closeViewer,
      onSelectNode: (id) => openViewer(id),
    }),
  );
}

// ---------------------------------------------------------------------------
// Backlog view — dependency DAG resolved into execution waves.
//
// Philosophy (HW-0024): a developer asking "what's next?" wants to see
// wave 0 at the top (no live blockers), wave 1 underneath, and so on.
// Parent edges still group sub-work: an epic appears at the wave where
// it itself sits and offers a "▼ N children" toggle to expand children
// *regardless of which wave those children live in* — parent grouping
// is orthogonal to wave ordering.
//
// Excluded nodes (cycles, unsatisfiable deps) surface in a visually
// distinct section at the bottom so they can't hide.
// ---------------------------------------------------------------------------

function BacklogView({ state, onSelect }) {
  const [collapsed, setCollapsed] = useState(() => new Set());

  // Index nodes + attach children = nodes whose `parent` points here.
  // We keep the raw node fields; only `children` is synthesised.
  const byId = useMemo(() => {
    const m = new Map();
    for (const n of state.nodes) m.set(n.id, { ...n, children: [] });
    for (const n of m.values()) {
      if (n.parent && m.has(n.parent)) m.get(n.parent).children.push(n);
    }
    for (const n of m.values()) {
      n.children.sort((a, b) => a.id.localeCompare(b.id));
    }
    return m;
  }, [state.nodes]);

  // Owner-by-node from the active claims stream (shown as a badge).
  const claimByNode = useMemo(() => {
    const m = new Map();
    for (const c of state.claims || []) {
      if (!m.has(c.node_id)) m.set(c.node_id, c.claimer || "claimed");
    }
    return m;
  }, [state.claims]);

  // UAT status by node — only populated for `needs-uat` nodes.
  const uatByNode = useMemo(() => {
    const m = new Map();
    for (const u of state.uat || []) m.set(u.id, u.uat_status);
    return m;
  }, [state.uat]);

  // The scheduler already returns waves sorted by (priority, id) and
  // excludes terminals. We just consume its output.
  const wavesPayload = state.waves || { waves: [], critical_path: [], excluded: [] };
  const waves = wavesPayload.waves || [];
  const excluded = wavesPayload.excluded || [];
  const critical = useMemo(() => new Set(wavesPayload.critical_path || []),
                           [wavesPayload.critical_path]);

  // Which node ids the scheduler actually placed? Used to decide whether
  // a child node shown under its parent is "done/archived" vs still in a
  // future wave. Terminal ids come from `already_done`.
  const terminalIds = useMemo(() => new Set(wavesPayload.already_done || []),
                              [wavesPayload.already_done]);
  const placedWave = useMemo(() => {
    const m = new Map();
    for (const w of waves) for (const id of w.nodes) m.set(id, w.n);
    return m;
  }, [waves]);

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
  if (waves.length === 0 && excluded.length === 0) {
    return h("div", { class: "empty" },
      "Nothing to schedule — every node is already terminal (done/archived/cancelled).");
  }

  // Render one node row. `depth` is parent-nesting indent (0 = top of a wave).
  // A node expanded here shows its children below it indented, regardless
  // of what wave those children themselves belong to.
  const renderRow = (id, depth) => {
    const n = byId.get(id);
    if (!n) {
      // Referenced but no graph entry — still surface it so it can't hide.
      return h("li", { key: id, class: "bl-row missing" },
        h("span", { class: "bl-caret leaf" }),
        h("span", { class: "bl-id", onClick: () => onSelect(id) }, id),
        h("span", { class: "bl-title muted" }, " (unknown node)"),
      );
    }
    const kids = n.children || [];
    const hasKids = kids.length > 0;
    const isCollapsed = collapsed.has(id);
    const owner = claimByNode.get(id) || n.owner || null;
    const uat = uatByNode.get(id);
    const onCrit = critical.has(id);

    return h("li", { key: id, class: "bl-row" + (depth > 0 ? " child" : "") },
      h("div", {
        class: "bl-line" + (onCrit ? " on-critical" : ""),
        style: depth ? `padding-left:${depth * 16}px` : "",
      },
        h("span", {
          class: "bl-caret" + (hasKids ? "" : " leaf"),
          onClick: () => hasKids && toggle(id),
          title: hasKids ? `${kids.length} child${kids.length === 1 ? "" : "ren"}` : "",
        }, hasKids ? (isCollapsed ? "▸" : "▾") : ""),
        h("span", { class: "bl-id", onClick: () => onSelect(id) }, id),
        h("span", { class: "bl-title", onClick: () => onSelect(id) }, " " + n.title),
        hasKids && h("span", { class: "bl-kidcount muted" },
          ` ${kids.length} child${kids.length === 1 ? "" : "ren"}`),
        h("span", { class: `badge status-${n.status}` }, n.status),
        n.priority && h("span", { class: "badge" }, n.priority),
        owner && h("span", { class: "badge owner", title: claimByNode.has(id) ? "currently claimed" : "owner" },
          "@" + String(owner).replace(/^@/, "")),
        uat && h("span", { class: `badge uat uat-${uat}` }, `uat:${uat}`),
        onCrit && h("span", { class: "badge crit", title: "on critical path" }, "critical"),
      ),
      hasKids && !isCollapsed && h("ul", { class: "bl-children" },
        kids.map((c) => renderRow(c.id, depth + 1))),
    );
  };

  return h("div", { class: "backlog" },
    h("div", { class: "muted bl-summary" },
      `${waves.length} wave${waves.length === 1 ? "" : "s"}`
      + `, depth ${wavesPayload.depth || waves.length}`
      + `, max width ${wavesPayload.max_width || 0}`
      + (wavesPayload.critical_path && wavesPayload.critical_path.length
          ? `, critical path: ${wavesPayload.critical_path.join(" → ")}`
          : "")),
    waves.map((w) => h("section", { key: `w${w.n}`, class: "bl-wave" },
      h("h3", null,
        h("span", { class: "bl-wave-n" }, `Wave ${w.n}`),
        h("span", { class: "muted" }, ` · ${w.nodes.length} node${w.nodes.length === 1 ? "" : "s"}`),
        w.n === 0 && h("span", { class: "muted bl-wave-hint" }, " · ready to start"),
      ),
      h("ul", { class: "bl-list" }, w.nodes.map((id) => renderRow(id, 0))),
    )),
    excluded.length > 0 && h("section", { class: "bl-wave bl-excluded" },
      h("h3", null,
        h("span", { class: "bl-wave-n" }, "Excluded"),
        h("span", { class: "muted" }, ` · ${excluded.length} node${excluded.length === 1 ? "" : "s"}`),
        h("span", { class: "muted bl-wave-hint" },
          " · cycle or unsatisfiable dependency — cannot schedule"),
      ),
      h("ul", { class: "bl-list" }, excluded.map((id) => renderRow(id, 0))),
    ),
  );
}

// ---------------------------------------------------------------------------
// Canvas view — see canvas.js (HW-0029).
// The HW-0023 D3-force canvas was removed: it rendered work items as
// nodes, which misframed the domain. The flow network is now the map
// (executors + routes), and work items are packets flowing through it.
// ---------------------------------------------------------------------------

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
// History view — reverse-chron done nodes, expandable dep tree, full-scope
// shared-dep indicator.
//
// Rules (HW-0037):
//  * Top-level list = every `done` node, newest close first.
//  * Expanding a row renders its `blocked_by` tree recursively; leaves or
//    already-visited-in-THIS-subtree cut recursion (local cycle guard).
//  * Shared-dep scope = the ENTIRE History view (list + every expanded tree).
//    Any node appearing more than once gets the link badge; second-and-later
//    appearances are muted + auto-collapse their own subtrees. Clicking a
//    muted appearance scrolls to the canonical (first) one.
// ---------------------------------------------------------------------------

async function fetchHistory(limit = 50, cursor = 0) {
  const r = await fetch(`/api/history?limit=${limit}&cursor=${cursor}`);
  if (!r.ok) throw new Error(`/api/history -> ${r.status}`);
  return r.json();
}

function fmtCloseDate(iso) {
  if (!iso) return "—";
  // Render as YYYY-MM-DD HH:MM (UTC). Keeps the columnar list tidy; if the
  // ts is unparseable we just fall back to the raw string.
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} `
       + `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

function HistoryView({ onSelect }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [expanded, setExpanded] = useState(() => new Set());
  // Canonical-appearance registry — keyed by node id, value is the DOM id
  // we use to scroll + highlight on shared-dep click.
  const canonicalRefs = useRef(new Map());
  const [highlightId, setHighlightId] = useState(null);

  useEffect(() => {
    let cancel = false;
    fetchHistory(50, 0).then((d) => { if (!cancel) setData(d); })
                       .catch((e) => { if (!cancel) setErr(String(e)); });
    return () => { cancel = true; };
  }, []);

  const toggle = useCallback((uid) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(uid)) next.delete(uid); else next.add(uid);
      return next;
    });
  }, []);

  // Compute, for the current render, how many times each node id appears
  // across the full view: every top-level item counts once, plus every node
  // reached by walking `blocked_by` through expanded rows (cycle-guarded per
  // walk). The first appearance (in render order) is canonical; all later
  // appearances are "secondary" → muted + auto-collapsed.
  //
  // We return:
  //   occurrence: Map<id, number>              — how many times it appears
  //   canonicalUid: Map<id, string>            — DOM id of first appearance
  // Consumers pass a per-rendering "appearance counter" to tell whether the
  // row they're rendering is the canonical one.
  const appearance = useMemo(() => {
    if (!data) return { counts: new Map(), order: [] };
    const counts = new Map();
    const order = [];          // render order ids (first appearance wins)
    const seenOnce = new Set();

    // Walk the blocked_by subtree of a top-level id for counting purposes,
    // respecting the "expanded" set (secondary appearances never recurse —
    // they're auto-collapsed). First-appearance rows recurse if the user
    // has expanded them. Cycle-guard: track visitedInWalk.
    const walk = (id, uid, visitedInWalk) => {
      const first = !seenOnce.has(id);
      counts.set(id, (counts.get(id) || 0) + 1);
      if (first) {
        seenOnce.add(id);
        order.push(id);
      }
      // Only recurse on the CANONICAL first appearance, and only if the user
      // has expanded this uid. Secondary appearances auto-collapse.
      if (!first) return;
      if (!expanded.has(uid)) return;
      if (visitedInWalk.has(id)) return;
      const nv = new Set(visitedInWalk); nv.add(id);
      const n = data.nodes[id];
      if (!n) return;
      const kids = n.blocked_by || [];
      for (const kid of kids) {
        walk(kid, `${uid}/${kid}`, nv);
      }
    };

    for (const item of data.items) {
      walk(item.id, `root/${item.id}`, new Set());
    }
    return { counts, order };
  }, [data, expanded]);

  const scrollToCanonical = useCallback((id) => {
    const uid = canonicalRefs.current.get(id);
    if (!uid) return;
    const el = document.querySelector(`[data-hist-uid="${CSS.escape(uid)}"]`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      setHighlightId(id);
      setTimeout(() => setHighlightId((cur) => (cur === id ? null : cur)), 1600);
    }
  }, []);

  // Reset the canonical-refs registry on every render pass; rendering will
  // re-populate it with fresh DOM ids reflecting the current expansion.
  canonicalRefs.current = new Map();

  if (err) return h("div", { class: "empty", style: "color: var(--err)" }, err);
  if (!data) return h("div", { class: "muted" }, "Loading history…");
  if (!data.items || data.items.length === 0) {
    return h("div", { class: "empty" }, "No closed nodes yet. Close one with `hopewell close`.");
  }

  // Recursive tree renderer. `uid` is the unique path id for a row so that
  // the same underlying node id can appear at multiple tree positions and
  // have independent expand state. `depth` drives indentation.
  const renderTree = (id, uid, depth, visitedInWalk) => {
    const n = data.nodes[id];
    const count = appearance.counts.get(id) || 0;
    const shared = count > 1;
    const firstAppearance = canonicalRefs.current.get(id) === undefined;
    // Register THIS render as canonical if it's the first time we see `id`.
    if (firstAppearance) canonicalRefs.current.set(id, uid);
    const muted = shared && !firstAppearance;

    if (!n) {
      return h("li", { key: uid, "data-hist-uid": uid, class: "hist-row unknown" },
        h("span", { class: "hist-caret leaf" }),
        h("span", { class: "hist-id", onClick: () => onSelect(id) }, id),
        h("span", { class: "muted" }, " (unknown)"),
      );
    }

    const kids = n.blocked_by || [];
    const hasKids = kids.length > 0;
    const isExpanded = expanded.has(uid) && !muted;   // secondary never expands
    const inCycle = visitedInWalk.has(id);
    const caret = !hasKids || inCycle
      ? ""
      : (isExpanded ? "▾" : "▸");
    const rowClasses = [
      "hist-row",
      "hist-tree-row",
      muted ? "hist-shared-secondary" : "",
      highlightId === id ? "hist-highlight" : "",
    ].filter(Boolean).join(" ");

    const clickId = (e) => {
      e.stopPropagation();
      if (muted) scrollToCanonical(id);
      else onSelect(id);
    };

    return h("li", { key: uid, "data-hist-uid": uid, class: rowClasses },
      h("div", {
        class: "hist-line",
        style: depth ? `padding-left:${depth * 16}px` : "",
      },
        h("span", {
          class: "hist-caret" + (!hasKids || inCycle ? " leaf" : ""),
          onClick: () => hasKids && !inCycle && !muted && toggle(uid),
          title: inCycle ? "cycle guard" : (hasKids ? `${kids.length} blocker${kids.length === 1 ? "" : "s"}` : ""),
        }, caret),
        h("span", { class: "hist-id", onClick: clickId }, n.id),
        h("span", { class: "hist-title", onClick: clickId }, " " + n.title),
        shared && h("span", {
          class: "hist-shared-badge",
          title: muted
            ? `also shown above (×${count}). Click to jump to canonical appearance.`
            : `shared — ×${count} across view`,
          onClick: (e) => { e.stopPropagation(); if (muted) scrollToCanonical(id); },
        }, "⇆"),
        h("span", { class: `badge status-${n.status}` }, n.status),
        inCycle && h("span", { class: "muted", style: "font-size:10px" }, " (cycle)"),
      ),
      hasKids && isExpanded && !inCycle && h("ul", { class: "hist-children" },
        kids.map((k) => {
          const nv = new Set(visitedInWalk); nv.add(id);
          return renderTree(k, `${uid}/${k}`, depth + 1, nv);
        })),
    );
  };

  return h("div", { class: "history" },
    h("div", { class: "muted hist-summary" },
      `${data.total} closed node${data.total === 1 ? "" : "s"}`
      + (data.next !== null ? ` — showing ${data.cursor + 1}..${data.cursor + data.items.length}` : "")),
    h("ul", { class: "hist-list" },
      data.items.map((item) => {
        const uid = `root/${item.id}`;
        const count = appearance.counts.get(item.id) || 0;
        const shared = count > 1;
        const firstAppearance = canonicalRefs.current.get(item.id) === undefined;
        if (firstAppearance) canonicalRefs.current.set(item.id, uid);
        const muted = shared && !firstAppearance;
        const isExpanded = expanded.has(uid) && !muted;
        const kids = item.blocked_by || [];
        const hasKids = kids.length > 0;

        return h("li", {
          key: uid,
          "data-hist-uid": uid,
          class: [
            "hist-row", "hist-top",
            muted ? "hist-shared-secondary" : "",
            highlightId === item.id ? "hist-highlight" : "",
          ].filter(Boolean).join(" "),
        },
          h("div", { class: "hist-top-line" },
            h("span", {
              class: "hist-caret" + (!hasKids ? " leaf" : ""),
              onClick: () => hasKids && !muted && toggle(uid),
            }, hasKids ? (isExpanded ? "▾" : "▸") : ""),
            h("span", {
              class: "hist-id",
              onClick: (e) => {
                e.stopPropagation();
                if (muted) scrollToCanonical(item.id);
                else onSelect(item.id);
              },
            }, item.id),
            h("span", { class: "hist-title" }, " " + item.title),
            shared && h("span", {
              class: "hist-shared-badge",
              title: muted
                ? `also shown above (×${count}). Click to jump.`
                : `shared — ×${count} across view`,
              onClick: (e) => { e.stopPropagation(); if (muted) scrollToCanonical(item.id); },
            }, "⇆"),
            h("span", { class: "hist-close-date", title: item.closed_at || "" },
              fmtCloseDate(item.closed_at)),
            item.closed_by && h("span", { class: "badge owner" },
              "@" + String(item.closed_by).replace(/^@/, "")),
            item.commit && h("span", { class: "badge hist-commit", title: item.commit },
              (item.commit || "").slice(0, 7)),
            (item.components || []).slice(0, 3).map((c) => h("span", {
              key: c, class: "hist-comp", style: `border-color:${colourFor(c)}; color:${colourFor(c)}`,
            }, c)),
            (item.components && item.components.length > 3) &&
              h("span", { class: "muted", style: "font-size:10px" }, `+${item.components.length - 3}`),
          ),
          hasKids && isExpanded && h("ul", { class: "hist-children" },
            kids.map((k) => renderTree(k, `${uid}/${k}`, 1, new Set([item.id]))),
          ),
        );
      })),
  );
}

// ---------------------------------------------------------------------------
// Detail side panel
// ---------------------------------------------------------------------------

// --- Detail helpers ---------------------------------------------------------

// Clickable node-id link: calls onSelect(id) instead of navigating.
function NodeLink({ id, onSelect }) {
  return h("a", {
    class: "node-link",
    href: "#",
    onClick: (e) => { e.preventDefault(); onSelect(id); },
  }, id);
}

// Join an array of ids into "LINK, LINK, LINK"; returns "—" when empty.
function NodeLinkList({ ids, onSelect }) {
  if (!ids || ids.length === 0) return h("span", { class: "muted" }, "—");
  const out = [];
  ids.forEach((id, i) => {
    if (i > 0) out.push(", ");
    out.push(h(NodeLink, { key: id, id, onSelect }));
  });
  return h(Fragment, null, ...out);
}

// Collapsible <details> block with an optional count badge.
function Collapsible({ summary, count, open, children }) {
  return h("details", { class: "det-block", open: !!open },
    h("summary", null,
      h("span", { class: "det-sum-title" }, summary),
      (count !== undefined) && h("span", { class: "det-sum-count" }, ` (${count})`),
    ),
    h("div", { class: "det-block-body" }, children),
  );
}

// Render the per-component data dict. Each key gets its own collapsible
// block; value is pretty-printed JSON. The special-cased `needs-uat` block
// is rendered by `UatBlock`, not here.
function ComponentDataView({ data }) {
  const keys = Object.keys(data || {}).filter((k) => k !== "needs-uat");
  if (keys.length === 0) return null;
  return h(Fragment, null,
    keys.sort().map((k) => h(Collapsible, {
      key: k, summary: k, open: true,
    },
      h("pre", { class: "code-json" }, JSON.stringify(data[k], null, 2)),
    )),
  );
}

// UAT block — status, checklist, verifier, verified_at, notes, failure.
function UatBlock({ uat }) {
  if (!uat) return null;
  const status = uat.status || "pending";
  const crits = Array.isArray(uat.acceptance_criteria) ? uat.acceptance_criteria : [];
  // Map UAT status -> default checkbox state. "passed" = all ticked,
  // everything else = unticked. Read-only; mutation lives in the UAT tab.
  const allTicked = status === "passed";
  return h("div", { class: `uat-block uat-${status}` },
    h("div", { class: "uat-head" },
      h("span", { class: "uat-label" }, "UAT"),
      h("span", { class: `status-chip ${status}` }, status),
      uat.verified_by && h("span", { class: "muted" }, ` by ${uat.verified_by}`),
      uat.verified_at && h("span", { class: "muted" }, ` @ ${uat.verified_at}`),
    ),
    crits.length > 0 && h("ul", { class: "uat-criteria" },
      crits.map((c, i) => h("li", { key: i },
        h("input", { type: "checkbox", checked: allTicked, disabled: true }),
        " ", c,
      ))),
    uat.notes && h("div", { class: "uat-notes" }, h("span", { class: "muted" }, "notes: "), uat.notes),
    uat.failure_reason && h("div", { class: "uat-fail" },
      h("span", { class: "muted" }, "failure_reason: "), uat.failure_reason),
  );
}

// Inputs panel: each input row shows from_node (as link) + artifact + kind
// + required flag + description.
function InputsList({ inputs, onSelect }) {
  if (!inputs || inputs.length === 0) return h("div", { class: "muted" }, "—");
  return h("ul", { class: "edge-list" },
    inputs.map((i, idx) => h("li", { key: idx },
      i.from_node
        ? h(NodeLink, { id: i.from_node, onSelect })
        : h("span", { class: "muted" }, "(no source)"),
      i.artifact && h("span", null, " · ", h("code", null, i.artifact)),
      i.kind && h("span", { class: "muted" }, ` [${i.kind}]`),
      i.required === false && h("span", { class: "muted" }, " (optional)"),
      i.description && h("div", { class: "muted edge-desc" }, i.description),
    )),
  );
}

// Outputs panel: path + kind + signal; no node-links (outputs are artifacts).
function OutputsList({ outputs }) {
  if (!outputs || outputs.length === 0) return h("div", { class: "muted" }, "—");
  return h("ul", { class: "edge-list" },
    outputs.map((o, idx) => h("li", { key: idx },
      h("code", null, o.path || "(no path)"),
      o.kind && h("span", { class: "muted" }, ` [${o.kind}]`),
      o.signal && h("span", { class: "muted" }, ` · signal: ${o.signal}`),
    )),
  );
}

// Clickable component chips.
function ComponentChips({ components }) {
  if (!components || components.length === 0) return h("span", { class: "muted" }, "—");
  return h("div", { class: "component-chips" },
    components.map((c) => h("span", {
      key: c,
      class: "comp-chip",
      style: `border-color:${colourFor(c)}; color:${colourFor(c)}`,
    }, c)),
  );
}

// Render markdown -> HTML via `marked`, then inject. `marked` escapes raw
// HTML so this is safe against ordinary content; worst case a malformed
// link renders as text.
function Markdown({ text }) {
  const html = useMemo(() => {
    if (!text) return "";
    try { return marked.parse(String(text)); }
    catch (e) { return `<pre>${String(text)}</pre>`; }
  }, [text]);
  return h("div", { class: "md", dangerouslySetInnerHTML: { __html: html } });
}

// --- Detail panel -----------------------------------------------------------

function Detail({ id, onSelect, onOpenViewer, onClose }) {
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
    const uat = node.component_data && node.component_data["needs-uat"];
    const hasUat = (node.components || []).includes("needs-uat") && uat;
    // Notes: newest-first. Server stores oldest-first in the markdown log
    // (human reads the story forward); in the panel we flip to newest-first
    // so the *latest status* is visible without scrolling — the common
    // "what just happened?" question when clicking a ticket.
    const notesNewestFirst = (node.notes || []).slice().reverse();

    content = h(Fragment, null,
      h("h2", null,
        h("span", { class: "det-id" }, node.id),
        " — ",
        h("span", { class: "det-title" }, node.title),
      ),
      onOpenViewer && h("div", { class: "det-actions" },
        h("button", {
          class: "det-view-doc",
          onClick: () => onOpenViewer(node.id),
          title: "Open full markdown viewer (#/doc/" + node.id + ")",
        }, "Open doc viewer →"),
      ),

      // Core metadata grid.
      h("dl", { class: "kv" },
        h("dt", null, "status"),    h("dd", null,
          h("span", { class: `status-chip ${node.status}` }, node.status)),
        h("dt", null, "priority"),  h("dd", null, node.priority || "—"),
        h("dt", null, "owner"),     h("dd", null, node.owner || "—"),
        h("dt", null, "project"),   h("dd", null, node.project || "—"),
        h("dt", null, "parent"),    h("dd", null,
          node.parent ? h(NodeLink, { id: node.parent, onSelect }) : "—"),
        h("dt", null, "created"),   h("dd", null, node.created || "—"),
        h("dt", null, "updated"),   h("dd", null, node.updated || "—"),
      ),

      // Components as chips.
      h("h3", null, "components"),
      h(ComponentChips, { components: node.components }),

      // UAT block — only when `needs-uat` present.
      hasUat && h(Fragment, null,
        h("h3", null, "UAT"),
        h(UatBlock, { uat }),
      ),

      // Edges: inputs / outputs / blocks / blocked_by / related.
      h("h3", null, "edges"),
      h("div", { class: "edges" },
        h("div", { class: "edge-group" },
          h("div", { class: "edge-label" }, "inputs"),
          h(InputsList, { inputs: node.inputs, onSelect })),
        h("div", { class: "edge-group" },
          h("div", { class: "edge-label" }, "outputs"),
          h(OutputsList, { outputs: node.outputs })),
        h("div", { class: "edge-group" },
          h("div", { class: "edge-label" }, "blocks"),
          h(NodeLinkList, { ids: node.blocks, onSelect })),
        h("div", { class: "edge-group" },
          h("div", { class: "edge-label" }, "blocked_by"),
          h(NodeLinkList, { ids: node.blocked_by, onSelect })),
        h("div", { class: "edge-group" },
          h("div", { class: "edge-label" }, "related"),
          h(NodeLinkList, { ids: node.related, onSelect })),
      ),

      // Per-component data (excl. needs-uat — shown above).
      node.component_data && Object.keys(node.component_data).some((k) => k !== "needs-uat") &&
        h(Fragment, null,
          h("h3", null, "component_data"),
          h(ComponentDataView, { data: node.component_data })),

      // Body (markdown rendered).
      node.body && node.body.trim() !== "" && h(Fragment, null,
        h("h3", null, "body"),
        h(Markdown, { text: node.body })),

      // Notes log — newest first.
      node.notes && node.notes.length > 0 && h(Fragment, null,
        h("h3", null, `notes (${node.notes.length}, newest first)`),
        h("ul", { class: "notes-log" },
          notesNewestFirst.map((entry, i) =>
            h("li", { key: i }, h(Markdown, { text: entry })))),
      ),
    );
  }

  render(content, body);
  return null;
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

render(h(App), document.getElementById("app"));
