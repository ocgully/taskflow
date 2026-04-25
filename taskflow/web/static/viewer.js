// Hopewell markdown viewer (HW-0032).
//
// Self-contained modal overlay that renders a node .md file plus its
// referenced spec slices with:
//
//   * Mermaid fences rendered inline via mermaid@11 from esm.sh.
//   * PlantUML / salt fences rendered via a client-side WASM build when
//     one is reachable; otherwise a graceful "rendering unavailable"
//     fallback card with the raw source (offline-only requirement — we
//     NEVER hit kroki/plantuml.com).
//   * Slice-aware spec rendering: for each `spec-input` reference, only
//     the pinned lines [start..end] are shown; surrounding context is
//     collapsed behind expand/collapse toggles.
//   * Drift detection: a slice whose stored slice_sha no longer matches
//     the live file gets a red DRIFT badge + the unified diff supplied
//     by the server, plus a "Re-pin" button that calls POST
//     /api/node/{id}/spec-repin and refreshes the view.
//
// Deep-link: the viewer reads/writes location.hash `#/doc/<NODE_ID>`
// (hybrid modal+route). app.js owns the hashchange listener; this
// module only exposes the DocViewer component.

import { h, render, Fragment } from "https://esm.sh/preact@10.22.0";
import { useState, useEffect, useMemo, useRef, useCallback }
  from "https://esm.sh/preact@10.22.0/hooks";
import { marked } from "https://esm.sh/marked@12.0.2";

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------

async function fetchDoc(id) {
  const r = await fetch(`/api/doc/${encodeURIComponent(id)}`);
  if (!r.ok) throw new Error(`/api/doc -> ${r.status}`);
  return r.json();
}

async function fetchSpec(path) {
  const url = `/api/spec?path=${encodeURIComponent(path)}`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`/api/spec -> ${r.status}`);
  return r.json();
}

async function repinSlice(nodeId, body) {
  const r = await fetch(`/api/node/${encodeURIComponent(nodeId)}/spec-repin`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`spec-repin -> ${r.status}`);
  return r.json();
}

// ---- Comment fetch helpers (HW-0033) -------------------------------------

async function fetchComments(target, status = "all") {
  const r = await fetch(
    `/api/comments/${encodeURIComponent(target)}?status=${encodeURIComponent(status)}`
  );
  if (!r.ok) throw new Error(`/api/comments -> ${r.status}`);
  return r.json();
}

async function postComment(body) {
  const r = await fetch(`/api/comments`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const txt = await r.text();
    throw new Error(`/api/comments POST -> ${r.status}: ${txt}`);
  }
  return r.json();
}

async function resolveComment(id, reason) {
  const r = await fetch(`/api/comments/${encodeURIComponent(id)}/resolve`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ reason }),
  });
  if (!r.ok) throw new Error(`resolve -> ${r.status}`);
  return r.json();
}

async function reopenComment(id) {
  const r = await fetch(`/api/comments/${encodeURIComponent(id)}/reopen`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({}),
  });
  if (!r.ok) throw new Error(`reopen -> ${r.status}`);
  return r.json();
}

async function promoteComment(id, title) {
  const r = await fetch(`/api/comments/${encodeURIComponent(id)}/promote`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!r.ok) {
    const txt = await r.text();
    throw new Error(`promote -> ${r.status}: ${txt}`);
  }
  return r.json();
}

// ---- Reconciliation (HW-0034) -------------------------------------------

async function fetchReconcileList({ consumer, specPath, status = "open" } = {}) {
  const params = new URLSearchParams();
  if (consumer) params.set("consumer", consumer);
  if (specPath) params.set("spec_path", specPath);
  if (status) params.set("status", status);
  const r = await fetch(`/api/reconcile/list?${params.toString()}`);
  if (!r.ok) throw new Error(`/api/reconcile/list -> ${r.status}`);
  return r.json();
}

async function reconcileQueue(body) {
  const r = await fetch(`/api/reconcile/queue`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const txt = await r.text();
    throw new Error(`/api/reconcile/queue -> ${r.status}: ${txt}`);
  }
  return r.json();
}

async function reconcileResolve(reviewId, body) {
  const r = await fetch(
    `/api/reconcile/${encodeURIComponent(reviewId)}/resolve`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }
  );
  if (!r.ok) {
    const txt = await r.text();
    throw new Error(`/api/reconcile/resolve -> ${r.status}: ${txt}`);
  }
  return r.json();
}

// ---------------------------------------------------------------------------
// Mermaid (lazy + singleton)
// ---------------------------------------------------------------------------

let _mermaidPromise = null;
function loadMermaid() {
  if (_mermaidPromise) return _mermaidPromise;
  _mermaidPromise = import("https://esm.sh/mermaid@11")
    .then((mod) => {
      const mermaid = mod.default || mod;
      try {
        mermaid.initialize({
          startOnLoad: false,
          theme: "dark",
          securityLevel: "strict",
        });
      } catch (_) { /* ignore */ }
      return mermaid;
    })
    .catch((e) => {
      _mermaidPromise = null;
      throw e;
    });
  return _mermaidPromise;
}

// ---------------------------------------------------------------------------
// PlantUML WASM (lazy, best-effort)
// ---------------------------------------------------------------------------
//
// PlantUML native is a JVM artifact. There is a community WASM build
// (ported via CheerpJ / TeaVM) available at the time of writing — we
// attempt to load it dynamically. If every candidate fails, we fall
// back to a code-block card labelled "plantuml — rendering unavailable".
// This is deliberately graceful: the viewer stays useful for spec
// review even without live PlantUML.
//
// Candidate packages attempted (first success wins):
//
//   1. @jcb91/plantuml-wasm (TeaVM port, exports a `render(text)` fn)
//   2. plantuml-wasm-renderer (lightweight wrapper)
//
// Each package is attempted via esm.sh's `?bundle` form to get a single
// fetch; failure is swallowed so the next candidate gets a go.

let _plantumlAttempted = false;
let _plantumlRenderer = null;          // fn(source) -> Promise<svgString>
let _plantumlError = null;

async function tryLoadPlantuml() {
  if (_plantumlAttempted) return _plantumlRenderer;
  _plantumlAttempted = true;

  const candidates = [
    // Try several plausible ESM packages; none may exist but we surface
    // a clean fallback if so. Order chosen to prefer WASM-backed renders
    // with no network calls at render time.
    {
      name: "plantuml-wasm",
      url: "https://esm.sh/plantuml-wasm?bundle",
      pick: (mod) => {
        const m = mod.default || mod;
        if (typeof m.renderSvg === "function")
          return (src) => m.renderSvg(src);
        if (typeof m.render === "function")
          return (src) => m.render(src);
        return null;
      },
    },
    {
      name: "@jcb91/plantuml-wasm",
      url: "https://esm.sh/@jcb91/plantuml-wasm?bundle",
      pick: (mod) => {
        const m = mod.default || mod;
        if (typeof m.renderSvg === "function")
          return (src) => m.renderSvg(src);
        return null;
      },
    },
  ];

  for (const cand of candidates) {
    try {
      const mod = await import(/* @vite-ignore */ cand.url);
      const fn = cand.pick(mod);
      if (fn) {
        _plantumlRenderer = async (src) => {
          const out = await fn(src);
          return typeof out === "string" ? out : (out && out.svg) || "";
        };
        console.info(`[hopewell] plantuml renderer: ${cand.name}`);
        return _plantumlRenderer;
      }
    } catch (e) {
      _plantumlError = e;
      // keep trying
    }
  }
  console.warn("[hopewell] no PlantUML WASM package loaded; using fallback",
               _plantumlError && String(_plantumlError));
  return null;
}

// ---------------------------------------------------------------------------
// Markdown rendering — custom marked renderer so mermaid / plantuml / salt
// fences emit placeholder DIVs we post-process after insertion.
// ---------------------------------------------------------------------------

function makeRenderer() {
  const renderer = new marked.Renderer();
  // marked 12: code(code, infoString, escaped). Handle either API.
  renderer.code = function (code, infoString) {
    const lang = (infoString || "").toLowerCase().split(/\s+/)[0] || "";
    const src = (code && typeof code === "object" && "text" in code)
      ? code.text : code;
    if (lang === "mermaid") {
      return `<div class="viewer-mermaid" data-src="${encodeURIComponent(src)}"></div>`;
    }
    if (lang === "plantuml" || lang === "puml" || lang === "salt") {
      return `<div class="viewer-plantuml" data-kind="${lang}" data-src="${encodeURIComponent(src)}"></div>`;
    }
    const escaped = String(src)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return `<pre class="code"><code class="lang-${lang}">${escaped}</code></pre>`;
  };
  return renderer;
}

function renderMarkdown(md) {
  try {
    return marked.parse(md, { renderer: makeRenderer(), gfm: true, breaks: false });
  } catch (e) {
    return `<pre class="code-err">${String(e)}</pre>`;
  }
}

// After inserting markdown HTML, upgrade placeholder DIVs in-place.
async function hydrateDiagrams(rootEl) {
  if (!rootEl) return;
  const mermaidEls = rootEl.querySelectorAll(".viewer-mermaid");
  const plantEls = rootEl.querySelectorAll(".viewer-plantuml");

  if (mermaidEls.length > 0) {
    try {
      const mermaid = await loadMermaid();
      let i = 0;
      for (const el of mermaidEls) {
        if (el.dataset.rendered === "1") continue;
        const src = decodeURIComponent(el.dataset.src || "");
        const id = `mm-${Date.now()}-${i++}`;
        try {
          const { svg } = await mermaid.render(id, src);
          el.innerHTML = svg;
          el.dataset.rendered = "1";
        } catch (e) {
          el.innerHTML = `<div class="diagram-err">mermaid render failed: ${String(e).replace(/</g, "&lt;")}</div><pre class="code">${src.replace(/</g, "&lt;")}</pre>`;
          el.dataset.rendered = "err";
        }
      }
    } catch (e) {
      // couldn't even load mermaid
      for (const el of mermaidEls) {
        if (el.dataset.rendered === "1") continue;
        const src = decodeURIComponent(el.dataset.src || "");
        el.innerHTML = `<div class="diagram-err">mermaid unavailable: ${String(e).replace(/</g, "&lt;")}</div><pre class="code">${src.replace(/</g, "&lt;")}</pre>`;
        el.dataset.rendered = "err";
      }
    }
  }

  if (plantEls.length > 0) {
    const renderer = await tryLoadPlantuml();
    for (const el of plantEls) {
      if (el.dataset.rendered === "1") continue;
      const src = decodeURIComponent(el.dataset.src || "");
      const kind = el.dataset.kind || "plantuml";
      if (!renderer) {
        el.innerHTML = `<div class="diagram-fallback"><span class="diagram-badge warn">${kind} — rendering unavailable (offline fallback)</span><pre class="code">${src.replace(/</g, "&lt;")}</pre></div>`;
        el.dataset.rendered = "fb";
        continue;
      }
      try {
        const svg = await renderer(src);
        if (svg && svg.trim().startsWith("<svg")) {
          el.innerHTML = svg;
        } else {
          el.innerHTML = `<div class="diagram-fallback"><span class="diagram-badge warn">${kind} — empty render</span><pre class="code">${src.replace(/</g, "&lt;")}</pre></div>`;
        }
        el.dataset.rendered = "1";
      } catch (e) {
        el.innerHTML = `<div class="diagram-fallback"><span class="diagram-badge warn">${kind} — render error</span><pre class="code">${src.replace(/</g, "&lt;")}</pre><div class="muted">${String(e).replace(/</g, "&lt;")}</div></div>`;
        el.dataset.rendered = "err";
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Spec slice panel
// ---------------------------------------------------------------------------
//
// Renders a single spec file with ONLY the pinned slices shown; context
// around and between slices is rendered as collapsed gap markers that
// expand on click.

function SpecSlices({ nodeId, specPath, refs, onNavigateSpec, onRepinned }) {
  const [spec, setSpec] = useState(null);
  const [err, setErr] = useState(null);
  const [expandedRanges, setExpandedRanges] = useState(() => new Set());
  const [pending, setPending] = useState(false);
  const mdHostRef = useRef(null);

  const load = useCallback(async () => {
    setErr(null);
    setSpec(null);
    try {
      const s = await fetchSpec(specPath);
      setSpec(s);
    } catch (e) {
      setErr(String(e));
    }
  }, [specPath]);

  useEffect(() => { load(); }, [load]);

  // Only the slices pinned by THIS node (the caller filters to the right path).
  const mySlices = useMemo(() => {
    // refs is prefiltered to spec_refs relevant to `specPath`.
    const out = [];
    for (const r of refs || []) {
      if (r.path !== specPath) continue;
      const rng = r.lines_now || r.lines || [1, 1];
      out.push({ ...r, _start: rng[0], _end: rng[1] });
    }
    out.sort((a, b) => a._start - b._start);
    return out;
  }, [refs, specPath]);

  // Build display fragments: for each slice, a slice panel. Between slices
  // and at the edges, a "gap" marker — click to expand (or always-open
  // if expandedRanges contains "gap-<idx>").
  const fragments = useMemo(() => {
    if (!spec) return [];
    const lines = spec.text.split("\n");
    // Strip a single trailing empty from a file ending in "\n".
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    const frags = [];
    let cursor = 1;
    mySlices.forEach((sl, i) => {
      const s = Math.max(1, sl._start);
      const e = Math.min(lines.length, sl._end);
      if (cursor < s) {
        frags.push({ kind: "gap", key: `gap-pre-${i}`, from: cursor, to: s - 1, lines });
      }
      frags.push({ kind: "slice", key: `slice-${i}`, slice: sl, from: s, to: e, lines });
      cursor = e + 1;
    });
    if (cursor <= lines.length) {
      frags.push({ kind: "gap", key: `gap-tail`, from: cursor, to: lines.length, lines });
    }
    return frags;
  }, [spec, mySlices]);

  // Render markdown to HTML when the expanded fragments change, then
  // hydrate diagrams inside the host.
  useEffect(() => {
    if (!mdHostRef.current) return;
    hydrateDiagrams(mdHostRef.current);
  });

  const toggleGap = useCallback((key) => {
    setExpandedRanges((prev) => {
      const n = new Set(prev);
      if (n.has(key)) n.delete(key); else n.add(key);
      return n;
    });
  }, []);

  const doRepin = useCallback(async (slice) => {
    if (pending) return;
    setPending(true);
    try {
      const body = slice.anchor
        ? { path: specPath, anchor: slice.anchor, why: slice.why }
        : { path: specPath, lines: slice.lines, why: slice.why };
      await repinSlice(nodeId, body);
      if (onRepinned) onRepinned();
      await load();
    } catch (e) {
      alert(`Re-pin failed: ${e}`);
    } finally {
      setPending(false);
    }
  }, [pending, nodeId, specPath, load, onRepinned]);

  if (err) return h("div", { class: "empty viewer-err" }, `Spec load error: ${err}`);
  if (!spec) return h("div", { class: "muted" }, `Loading ${specPath}…`);

  if (mySlices.length === 0) {
    return h("div", { class: "muted" }, "No slices pinned on this spec.");
  }

  return h("div", { class: "spec-panel" },
    h("div", { class: "spec-path" },
      h("span", { class: "muted" }, "spec: "),
      h("a", {
        href: "#", class: "spec-link",
        onClick: (e) => { e.preventDefault(); if (onNavigateSpec) onNavigateSpec(specPath); },
      }, specPath),
      h("span", { class: "muted" }, `  (${spec.line_count} lines)`),
    ),
    h("div", { class: "md viewer-md", ref: mdHostRef },
      fragments.map((f) => {
        if (f.kind === "gap") {
          const isOpen = expandedRanges.has(f.key);
          const chunk = f.lines.slice(f.from - 1, f.to).join("\n");
          return h("div", { key: f.key, class: "slice-gap" },
            h("button", {
              class: "slice-gap-btn",
              onClick: () => toggleGap(f.key),
            }, isOpen
              ? `▼ hide lines L${f.from}-L${f.to}`
              : `▶ show context (lines L${f.from}-L${f.to}, ${f.to - f.from + 1} lines)`),
            isOpen && h("div", {
              class: "slice-gap-body",
              dangerouslySetInnerHTML: { __html: renderMarkdown(chunk) },
            }),
          );
        }
        // kind === slice
        const sl = f.slice;
        const chunk = f.lines.slice(f.from - 1, f.to).join("\n");
        const state = sl.state || "unknown";
        const drifted = state === "drift" || state === "anchor-lost" || state === "missing";
        return h("div", {
            key: f.key,
            class: `slice-block ${drifted ? "drifted" : "clean"}`,
          },
          h("div", { class: "slice-head" },
            h("span", { class: "slice-head-label" },
              sl.anchor ? sl.anchor : `lines L${f.from}-L${f.to}`),
            h("span", {
              class: `slice-badge ${drifted ? "warn" : "ok"}`,
            }, drifted ? (state === "drift" ? "DRIFT" : state.toUpperCase()) : "pinned"),
            sl.why && h("span", { class: "slice-why muted" }, `— ${sl.why}`),
            drifted && h("button", {
              class: "slice-repin",
              disabled: pending,
              onClick: () => doRepin(sl),
              title: "Re-record slice hash against current file content",
            }, pending ? "…" : "Re-pin"),
          ),
          h("div", {
            class: "slice-body",
            dangerouslySetInnerHTML: { __html: renderMarkdown(chunk) },
          }),
          drifted && sl.patch && h("details", { class: "slice-diff", open: true },
            h("summary", null, "drift — unified diff"),
            h("pre", { class: "code diff-body" }, sl.patch),
          ),
        );
      }),
    ),
  );
}

// ---------------------------------------------------------------------------
// Spec overlay (secondary view opened by clicking a spec path)
// ---------------------------------------------------------------------------

function SpecOverlay({ path, onBack }) {
  const [spec, setSpec] = useState(null);
  const [err, setErr] = useState(null);
  const hostRef = useRef(null);

  useEffect(() => {
    let cancel = false;
    setSpec(null); setErr(null);
    fetchSpec(path).then((s) => { if (!cancel) setSpec(s); })
                   .catch((e) => { if (!cancel) setErr(String(e)); });
    return () => { cancel = true; };
  }, [path]);

  useEffect(() => { hydrateDiagrams(hostRef.current); });

  let body;
  if (err) body = h("div", { class: "empty" }, String(err));
  else if (!spec) body = h("div", { class: "muted" }, `Loading ${path}…`);
  else body = h("div", { class: "md viewer-md", ref: hostRef,
                         dangerouslySetInnerHTML: { __html: renderMarkdown(spec.text) } });

  const consumers = (spec && spec.consumers) || [];

  // HW-0034: count drifted slices across consumers — drives the
  // "Queue downstream reviews (N)" button.
  const driftedCount = useMemo(() => {
    let n = 0;
    for (const c of consumers) {
      for (const sl of (c.slices || [])) {
        const st = sl.state || "unknown";
        if (st === "drift" || st === "anchor-lost" || st === "missing") n++;
      }
    }
    return n;
  }, [consumers]);

  return h("div", { class: "viewer-spec-overlay" },
    h("div", { class: "viewer-spec-head" },
      h("button", { class: "viewer-back", onClick: onBack }, "← back"),
      h("span", { class: "viewer-spec-title" }, path),
    ),
    consumers.length > 0 && h("div", { class: "viewer-consumers" },
      h("span", { class: "muted" }, "pinned by: "),
      consumers.map((c, i) => h(Fragment, { key: c.node },
        i > 0 && ", ",
        h("span", { class: "node-chip" }, c.node),
        h("span", { class: "muted" }, ` (${c.slices.length})`),
      )),
    ),
    // HW-0034: spec-edit-side button + downstream-review summary panel
    spec && h(ReconcilePanel, { specPath: path, driftedCount }),
    body,
    // HW-0033: comment threads anchored on this spec file
    spec && h(CommentsPanel, { target: path, markdown: spec.text || "" }),
  );
}

// ---------------------------------------------------------------------------
// Reconciliation panel (HW-0034)
// ---------------------------------------------------------------------------
//
// Sits between the consumers strip and the spec body. Two sub-features:
//
//   1. "Queue downstream reviews (N)" button — fires Trigger A across
//      every drifted slice consumed off this spec. Disabled when the
//      drifted-count is zero.
//   2. A tiny list of OPEN downstream-review nodes for this spec, each
//      with the four resolution buttons (no-impact, in-scope,
//      out-of-scope, spec-revert) inline.

function ReconcilePanel({ specPath, driftedCount }) {
  const [reviews, setReviews] = useState([]);
  const [pending, setPending] = useState(false);
  const [err, setErr] = useState(null);
  const [info, setInfo] = useState(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const r = await fetchReconcileList({ specPath, status: "open" });
      setReviews(r.reviews || []);
    } catch (e) {
      setErr(String(e));
    }
  }, [specPath]);

  useEffect(() => { load(); }, [load]);

  const queueAll = useCallback(async () => {
    if (pending) return;
    setPending(true); setErr(null); setInfo(null);
    try {
      const res = await reconcileQueue({ spec_path: specPath });
      const created = res.created || 0;
      const skipped = res.skipped_existing || 0;
      setInfo(
        `Queued ${created} new review(s)`
          + (skipped ? `, ${skipped} already open` : "")
          + ` for ${specPath}.`
      );
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setPending(false);
    }
  }, [pending, specPath, load]);

  const resolve = useCallback(async (review, outcome) => {
    if (pending) return;
    let followupTitle = null;
    let notes = null;
    if (outcome === "update-out-of-scope") {
      followupTitle = window.prompt(
        "Title for the follow-up work item that will block "
          + (review.consumer_node || "the consumer") + ":",
        `Follow up on spec change for ${review.consumer_node}`,
      );
      if (!followupTitle) return;
    }
    notes = window.prompt(`Resolution notes (optional, outcome=${outcome}):`, "")
      || null;
    setPending(true); setErr(null); setInfo(null);
    try {
      const body = { outcome };
      if (notes) body.notes = notes;
      if (followupTitle) body.followup_title = followupTitle;
      await reconcileResolve(review.review_node, body);
      setInfo(`Resolved ${review.review_node} (${outcome}).`);
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setPending(false);
    }
  }, [pending, load]);

  const btnLabel = driftedCount > 0
    ? `Queue downstream reviews (${driftedCount})`
    : "Queue downstream reviews (0 — clean)";

  return h("div", { class: "reconcile-panel" },
    h("div", { class: "reconcile-actions" },
      h("button", {
        class: "reconcile-queue-btn",
        disabled: pending || driftedCount === 0,
        onClick: queueAll,
        title: driftedCount === 0
          ? "No drifted slices on this spec — nothing to queue"
          : `Create downstream-review nodes for the ${driftedCount} drifted slice(s)`,
      }, pending ? "queuing…" : btnLabel),
      err && h("span", { class: "reconcile-err" }, err),
      info && h("span", { class: "reconcile-info muted" }, info),
    ),
    reviews.length > 0 && h("div", { class: "reconcile-reviews" },
      h("div", { class: "reconcile-reviews-head muted" },
        `${reviews.length} open downstream-review(s):`),
      reviews.map((r) => {
        const slice = r.slice || {};
        const where = slice.anchor
          ? slice.anchor
          : (slice.lines ? `L${slice.lines[0]}-L${slice.lines[1]}` : "?");
        return h("div", { key: r.review_node, class: "reconcile-review-card" },
          h("div", { class: "reconcile-review-head" },
            h("span", { class: "node-chip" }, r.review_node),
            h("span", { class: "muted" },
              ` blocks ${r.consumer_node} — slice ${where} (${r.trigger})`),
          ),
          h("div", { class: "reconcile-resolve-row" },
            h("button", {
              class: "reconcile-btn", disabled: pending,
              onClick: () => resolve(r, "no-impact"),
              title: "Slice changed but semantics unchanged — re-pin slice_sha",
            }, "no-impact (re-pin)"),
            h("button", {
              class: "reconcile-btn", disabled: pending,
              onClick: () => resolve(r, "update-in-scope"),
              title: "Consumer absorbs the new spec into its current scope",
            }, "in-scope"),
            h("button", {
              class: "reconcile-btn", disabled: pending,
              onClick: () => resolve(r, "update-out-of-scope"),
              title: "Spawn a follow-up work item that blocks the consumer",
            }, "out-of-scope (spawn follow-up)"),
            h("button", {
              class: "reconcile-btn", disabled: pending,
              onClick: () => resolve(r, "spec-revert"),
              title: "Spec change was wrong — record the decision (manual revert)",
            }, "spec-revert"),
          ),
        );
      }),
    ),
  );
}

// ---------------------------------------------------------------------------
// Comments panel (HW-0033)
// ---------------------------------------------------------------------------
//
// Appended below the markdown body. Shows open threads by default; toggle
// to include resolved. "Add comment" composer posts to whatever the
// current target is (a node id — spec-file comments are exposed via the
// same endpoint when the viewer is in spec-overlay mode).
//
// Anchor controls on the composer are intentionally minimal: whole-file
// vs heading-section (pick a heading from the doc) vs line-range (enter
// manually). The point here is to exercise every path from the UI; a
// fancier gutter/line-picker can come later without touching the
// endpoint contract.
//
// Each thread card surfaces its anchor state ("resolved" / "drifted" /
// "orphaned") so the user immediately sees when an anchor has slid.

function AnchorBadge({ anchor, reconciled }) {
  const state = (reconciled && reconciled._state) || "unknown";
  const cls = state === "orphaned"
    ? "comment-anchor-badge warn"
    : state === "drifted"
      ? "comment-anchor-badge drift"
      : "comment-anchor-badge";
  const label = state === "orphaned"
    ? "NEEDS RE-ANCHOR"
    : state === "drifted" ? "drifted" : "pinned";
  const typ = (anchor && anchor.type) || "whole-file";
  let where = "(whole-file)";
  if (typ === "heading-section") {
    where = `#${anchor.heading_slug || "?"}`;
  } else if (typ === "line-range") {
    const ln = (reconciled && reconciled.lines) || anchor.lines || [0, 0];
    where = `L${ln[0]}-L${ln[1]}`;
  }
  return h("span", { class: cls }, `${where} · ${label}`);
}

function CommentCard({ thread, onResolve, onReopen, onPromote, pending }) {
  const [showPromote, setShowPromote] = useState(false);
  const [title, setTitle] = useState(`Address comment ${thread.id}`);
  const anchor = thread.anchor || {};
  const reconciled = thread.reconciled_anchor || {};
  const resolved = !!thread.resolved;

  return h("div", { class: `comment-card ${resolved ? "resolved" : ""}` },
    h("div", { class: "comment-head" },
      h("span", { class: "comment-author" }, thread.actor || "(anon)"),
      h("span", { class: "comment-ts muted" }, thread.ts),
      h(AnchorBadge, { anchor, reconciled }),
      resolved && h("span", { class: "comment-state-badge resolved" }, "resolved"),
    ),
    h("div", { class: "comment-body", dangerouslySetInnerHTML: {
      __html: renderMarkdown(thread.body || ""),
    } }),
    thread.resolve_reason && h("div", { class: "comment-resolve-reason muted" },
      `resolved: ${thread.resolve_reason}`),
    h("div", { class: "comment-actions" },
      !resolved && h("button", {
        class: "comment-btn",
        disabled: pending,
        onClick: () => {
          const reason = window.prompt("resolve reason (optional)") || "";
          onResolve(thread.id, reason);
        },
      }, "Resolve"),
      resolved && h("button", {
        class: "comment-btn",
        disabled: pending,
        onClick: () => onReopen(thread.id),
      }, "Reopen"),
      h("button", {
        class: "comment-btn",
        disabled: pending,
        onClick: () => setShowPromote((v) => !v),
      }, showPromote ? "Cancel promote" : "Promote to review"),
    ),
    showPromote && h("div", { class: "comment-promote" },
      h("input", {
        class: "comment-promote-title",
        type: "text",
        value: title,
        onInput: (ev) => setTitle(ev.target.value),
        placeholder: "Review-node title…",
      }),
      h("button", {
        class: "comment-btn primary",
        disabled: pending || !title.trim(),
        onClick: async () => {
          const ok = await onPromote(thread.id, title.trim());
          if (ok) setShowPromote(false);
        },
      }, pending ? "…" : "Create review"),
    ),
  );
}

function CommentComposer({ target, onPosted, headings }) {
  const [anchorKind, setAnchorKind] = useState("whole-file");
  const [headingSlug, setHeadingSlug] = useState("");
  const [lineRange, setLineRange] = useState("");
  const [body, setBody] = useState("");
  const [pending, setPending] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!headingSlug && headings && headings.length > 0) {
      setHeadingSlug(headings[0].slug);
    }
  }, [headings, headingSlug]);

  const submit = useCallback(async () => {
    if (!body.trim()) return;
    setPending(true); setErr(null);
    const anchor = { type: anchorKind };
    if (anchorKind === "heading-section") {
      if (!headingSlug) { setErr("pick a heading"); setPending(false); return; }
      anchor.heading = headingSlug;
    } else if (anchorKind === "line-range") {
      const m = lineRange.match(/^\s*(\d+)\s*-\s*(\d+)\s*$/);
      if (!m) { setErr("lines must be like 45-72"); setPending(false); return; }
      anchor.lines = [parseInt(m[1], 10), parseInt(m[2], 10)];
    }
    try {
      await postComment({ target, body, anchor });
      setBody("");
      setLineRange("");
      if (onPosted) onPosted();
    } catch (e) {
      setErr(String(e));
    } finally {
      setPending(false);
    }
  }, [target, body, anchorKind, headingSlug, lineRange, onPosted]);

  return h("div", { class: "comment-composer" },
    h("div", { class: "composer-anchor-row" },
      h("label", null,
        h("input", {
          type: "radio", name: "anchor-kind", value: "whole-file",
          checked: anchorKind === "whole-file",
          onChange: () => setAnchorKind("whole-file"),
        }),
        " whole file",
      ),
      h("label", null,
        h("input", {
          type: "radio", name: "anchor-kind", value: "heading-section",
          checked: anchorKind === "heading-section",
          onChange: () => setAnchorKind("heading-section"),
        }),
        " heading",
      ),
      h("label", null,
        h("input", {
          type: "radio", name: "anchor-kind", value: "line-range",
          checked: anchorKind === "line-range",
          onChange: () => setAnchorKind("line-range"),
        }),
        " lines",
      ),
      anchorKind === "heading-section" && h("select", {
        class: "composer-heading-select",
        value: headingSlug,
        onChange: (ev) => setHeadingSlug(ev.target.value),
      },
        (headings || []).length === 0
          ? h("option", { value: "" }, "(no headings)")
          : (headings || []).map((h2) =>
              h("option", { key: h2.slug, value: h2.slug },
                `${"#".repeat(h2.level)} ${h2.text}`)),
      ),
      anchorKind === "line-range" && h("input", {
        class: "composer-line-input",
        type: "text",
        placeholder: "e.g. 45-72",
        value: lineRange,
        onInput: (ev) => setLineRange(ev.target.value),
      }),
    ),
    h("textarea", {
      class: "composer-body",
      placeholder: "Write a comment… (markdown ok)",
      value: body,
      onInput: (ev) => setBody(ev.target.value),
      rows: 3,
    }),
    h("div", { class: "composer-actions" },
      err && h("span", { class: "comment-err" }, err),
      h("button", {
        class: "comment-btn primary",
        disabled: pending || !body.trim(),
        onClick: submit,
      }, pending ? "posting…" : "Post comment"),
    ),
  );
}

function extractHeadings(markdown) {
  // Simple scan for ATX headings (# .. ######). Slug matches comment.py.
  const out = [];
  if (!markdown) return out;
  const lines = markdown.split("\n");
  for (const line of lines) {
    const m = /^(#{1,6})\s+(.+?)\s*$/.exec(line);
    if (!m) continue;
    const text = m[2].trim();
    const slug = text.toLowerCase()
      .replace(/[^a-z0-9\- ]+/g, "")
      .replace(/\s+/g, "-")
      .replace(/-+/g, "-")
      .replace(/^-+|-+$/g, "");
    out.push({ level: m[1].length, text, slug });
  }
  return out;
}

function CommentsPanel({ target, markdown }) {
  const [bundle, setBundle] = useState(null);
  const [err, setErr] = useState(null);
  const [statusFilter, setStatusFilter] = useState("open");
  const [pendingId, setPendingId] = useState(null);

  const refresh = useCallback(async () => {
    setErr(null);
    try {
      const d = await fetchComments(target, "all");
      setBundle(d);
    } catch (e) {
      setErr(String(e));
    }
  }, [target]);

  useEffect(() => { refresh(); }, [refresh]);

  const headings = useMemo(() => extractHeadings(markdown || ""), [markdown]);

  const threads = useMemo(() => {
    const all = (bundle && bundle.threads) || [];
    if (statusFilter === "open") return all.filter((t) => !t.resolved);
    if (statusFilter === "resolved") return all.filter((t) => t.resolved);
    return all;
  }, [bundle, statusFilter]);

  const openCount = useMemo(() => {
    const all = (bundle && bundle.threads) || [];
    return all.filter((t) => !t.resolved).length;
  }, [bundle]);
  const orphanCount = useMemo(() => {
    const all = (bundle && bundle.threads) || [];
    return all.filter((t) => (t.reconciled_anchor || {})._state === "orphaned").length;
  }, [bundle]);

  const doResolve = useCallback(async (id, reason) => {
    setPendingId(id);
    try { await resolveComment(id, reason); await refresh(); }
    catch (e) { alert(`resolve failed: ${e}`); }
    finally { setPendingId(null); }
  }, [refresh]);

  const doReopen = useCallback(async (id) => {
    setPendingId(id);
    try { await reopenComment(id); await refresh(); }
    catch (e) { alert(`reopen failed: ${e}`); }
    finally { setPendingId(null); }
  }, [refresh]);

  const doPromote = useCallback(async (id, title) => {
    setPendingId(id);
    try {
      const res = await promoteComment(id, title);
      await refresh();
      alert(`Created review node ${res.review_node} (references ${res.references && res.references.to})`);
      return true;
    } catch (e) {
      alert(`promote failed: ${e}`);
      return false;
    } finally {
      setPendingId(null);
    }
  }, [refresh]);

  return h("div", { class: "comments-panel" },
    h("div", { class: "comments-head" },
      h("h3", null, "Comments"),
      h("span", { class: "muted comments-stats" },
        `${openCount} open`,
        orphanCount > 0 ? `, ${orphanCount} need re-anchor` : "",
      ),
      h("span", { class: "comments-filter" },
        ["open", "resolved", "all"].map((s) => h("button", {
          key: s,
          class: `comment-filter-btn ${statusFilter === s ? "active" : ""}`,
          onClick: () => setStatusFilter(s),
        }, s)),
      ),
    ),
    err && h("div", { class: "comment-err" }, err),
    h(CommentComposer, { target, onPosted: refresh, headings }),
    threads.length === 0
      ? h("div", { class: "muted comments-empty" },
          `No ${statusFilter === "all" ? "" : statusFilter + " "}comments yet.`)
      : threads.map((t) => h(CommentCard, {
          key: t.id,
          thread: t,
          pending: pendingId === t.id,
          onResolve: doResolve,
          onReopen: doReopen,
          onPromote: doPromote,
        })),
  );
}

// ---------------------------------------------------------------------------
// Root viewer component
// ---------------------------------------------------------------------------

export function DocViewer({ nodeId, onClose, onSelectNode }) {
  const [doc, setDoc] = useState(null);
  const [err, setErr] = useState(null);
  const [refreshTick, setRefreshTick] = useState(0);
  const [overlaySpec, setOverlaySpec] = useState(null);
  const mdHostRef = useRef(null);

  const refresh = useCallback(() => setRefreshTick((t) => t + 1), []);

  useEffect(() => {
    let cancel = false;
    setDoc(null); setErr(null);
    fetchDoc(nodeId).then((d) => { if (!cancel) setDoc(d); })
                    .catch((e) => { if (!cancel) setErr(String(e)); });
    return () => { cancel = true; };
  }, [nodeId, refreshTick]);

  // Hydrate diagrams in the node-body markdown after render.
  useEffect(() => { hydrateDiagrams(mdHostRef.current); });

  // Escape closes. Body scroll-lock so page behind doesn't scroll.
  useEffect(() => {
    const onKey = (ev) => {
      if (ev.key === "Escape") {
        if (overlaySpec) setOverlaySpec(null);
        else onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose, overlaySpec]);

  // Group spec_refs by file so we render one panel per spec file.
  const refsByPath = useMemo(() => {
    const out = new Map();
    if (!doc || !doc.spec_refs) return out;
    for (const r of doc.spec_refs) {
      if (!r.path) continue;
      if (!out.has(r.path)) out.set(r.path, []);
      out.get(r.path).push(r);
    }
    return out;
  }, [doc]);

  let body;
  if (err) {
    body = h("div", { class: "empty viewer-err" }, `Doc load error: ${err}`);
  } else if (!doc) {
    body = h("div", { class: "muted" }, `Loading ${nodeId}…`);
  } else {
    const node = doc.node || {};
    body = h(Fragment, null,
      h("div", { class: "viewer-head" },
        h("h2", null,
          h("span", { class: "det-id" }, node.id || nodeId),
          node.title ? h(Fragment, null, " — ", h("span", null, node.title)) : null,
        ),
        h("div", { class: "viewer-meta muted" },
          h("span", null, doc.node_md_path || ""),
          node.status && h("span", { class: `status-chip ${node.status}` }, node.status),
        ),
      ),
      h("div", { class: "md viewer-md", ref: mdHostRef,
                 dangerouslySetInnerHTML: { __html: renderMarkdown(doc.markdown || "") } }),
      refsByPath.size > 0 && h("div", { class: "viewer-specs" },
        h("h3", null, "Referenced specs"),
        Array.from(refsByPath.entries()).map(([path, refs]) =>
          h(SpecSlices, {
            key: path,
            nodeId,
            specPath: path,
            refs,
            onNavigateSpec: (p) => setOverlaySpec(p),
            onRepinned: refresh,
          }),
        ),
      ),
      // HW-0033: comment threads anchored on this node's .md
      h(CommentsPanel, { target: nodeId, markdown: doc.markdown || "" }),
    );
  }

  return h("div", {
    class: "viewer-root",
    role: "dialog",
    "aria-modal": "true",
    onClick: (e) => { if (e.target === e.currentTarget) onClose(); },
  },
    h("div", { class: "viewer-modal" },
      h("button", {
        class: "viewer-close", "aria-label": "close", onClick: onClose,
      }, "×"),
      overlaySpec
        ? h(SpecOverlay, { path: overlaySpec, onBack: () => setOverlaySpec(null) })
        : body,
    ),
  );
}

export default DocViewer;
