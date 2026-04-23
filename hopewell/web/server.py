"""FastAPI server + SSE bridge for the Hopewell local web UI.

Design notes:

* Optional extra. All web-only imports (fastapi, uvicorn, watchdog) live
  behind a lazy guard so that someone who installed plain `hopewell`
  without `[web]` gets a clear, single-line error — not a traceback.
* Read-mostly. All mutations still go through `hopewell.project.Project`
  (i.e. the same code path the CLI uses); the server never touches
  `.hopewell/` files directly, it just reflects them back over HTTP.
* Realtime via watchdog. A FileSystemEventHandler watches
  `.hopewell/events.jsonl` for modifications and fans new tail-lines out
  to any open SSE subscriber queue.
* Static frontend. No build step — `static/index.html` loads Preact + D3
  as ES modules from esm.sh. The server only serves the three static
  files and the JSON API.

CLI entry: `hopewell.web.server.run(project_root, port, open_browser)`.
cli.py wires `hopewell web` to this; we don't touch cli.py from here.

NOTE: we deliberately do NOT use `from __future__ import annotations`
here. FastAPI uses `typing.get_type_hints()` to resolve parameter types
for dependency injection; with stringified annotations it evaluates
them in the module's globals, where `Request` is not visible because
web-only imports live inside `create_app`. Keeping annotations live
sidesteps that trap.
"""

import asyncio
import json
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Optional-extra guard
# ---------------------------------------------------------------------------

_MISSING_EXTRAS_HINT = (
    "Hopewell's web UI requires the `[web]` extra.\n"
    "Install it with:\n"
    "    pip install 'hopewell[web]'\n"
    "(brings in fastapi, uvicorn, watchdog)."
)


def _require_web_extras() -> None:
    missing: List[str] = []
    for mod in ("fastapi", "uvicorn", "watchdog"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        sys.stderr.write(
            f"hopewell web: missing extras ({', '.join(missing)}).\n"
            f"{_MISSING_EXTRAS_HINT}\n"
        )
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------


class EventBus:
    """Tiny in-memory fan-out. One queue per connected SSE subscriber.

    We keep the queues bounded so a slow client can't eat all our RAM —
    if a queue is full we drop the oldest message (reconnect will
    resync via /api/state anyway).
    """

    def __init__(self, max_queue: int = 256) -> None:
        self._subscribers: "List[asyncio.Queue[str]]" = []
        self._lock = asyncio.Lock()
        self._max_queue = max_queue

    async def subscribe(self) -> "asyncio.Queue[str]":
        q: "asyncio.Queue[str]" = asyncio.Queue(maxsize=self._max_queue)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: "asyncio.Queue[str]") -> None:
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    async def publish(self, payload: str) -> None:
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # drop oldest; clients reconnect-and-resync is fine
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Events.jsonl tailer
# ---------------------------------------------------------------------------


class EventsTailer:
    """Watchdog-driven tail of `.hopewell/events.jsonl`.

    Remembers byte offset of last-sent line; on file-modified, reads
    new lines and hands each (parsed) event to `on_event`. Robust to
    file rotation (offset reset on shrink).
    """

    def __init__(self, events_path: Path, on_event) -> None:
        self.events_path = events_path
        self.on_event = on_event          # callable(dict)
        self._offset = 0
        self._lock = threading.Lock()
        # Seed offset at EOF so we only emit *new* events after startup.
        if events_path.is_file():
            self._offset = events_path.stat().st_size

    def drain_new(self) -> None:
        """Read any new bytes and fire on_event for each new line."""
        with self._lock:
            if not self.events_path.is_file():
                return
            size = self.events_path.stat().st_size
            if size < self._offset:
                # Rotation / truncation — start from 0.
                self._offset = 0
            if size == self._offset:
                return
            try:
                with self.events_path.open("rb") as f:
                    f.seek(self._offset)
                    chunk = f.read()
                    self._offset = f.tell()
            except OSError:
                return
        # Parse + dispatch outside the lock.
        for raw in chunk.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                ev = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            try:
                self.on_event(ev)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _load_project(project_root: Path):
    """Load a Hopewell project, surface a readable error if `.hopewell/` is absent."""
    from hopewell.project import Project
    try:
        return Project.load(project_root)
    except SystemExit:
        raise
    except Exception as e:
        raise SystemExit(
            f"hopewell web: could not load project at {project_root}: {e}\n"
            f"Run `hopewell init` in a project directory first."
        )


def _state_snapshot(project) -> Dict[str, Any]:
    """Compose the /api/state payload — nodes, edges, UAT, claims, waves."""
    from hopewell import query as query_mod
    from hopewell import uat as uat_mod

    graph = query_mod.graph(project)
    # Derive "systems" = distinct parent chains (roots + groupings).
    roots: List[Dict[str, Any]] = []
    by_id = {n["id"]: n for n in graph["nodes"]}
    for n in graph["nodes"]:
        if not n.get("parent"):
            roots.append({"id": n["id"], "title": n["title"], "status": n["status"]})

    try:
        uat_items = uat_mod.list_uat(project, status="all")
    except Exception:
        uat_items = []

    try:
        claim_items = query_mod.claims(project).get("claims", [])
    except Exception:
        claim_items = []

    try:
        waves_payload = query_mod.waves(project)["stack"]
    except Exception:
        waves_payload = {"waves": [], "critical_path": [], "depth": 0, "max_width": 0}

    return {
        "project": {
            "name": project.cfg.name,
            "root": str(project.root),
            "id_prefix": project.cfg.id_prefix,
        },
        "systems": roots,
        "nodes": graph["nodes"],
        "edges": graph["edges"],
        "uat": uat_items,
        "claims": claim_items,
        "waves": waves_payload,
    }


def create_app(project_root: Path):
    """Build the FastAPI app. Called by `run`; factored for tests."""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.requests import Request

    project_root = project_root.resolve()
    project = _load_project(project_root)

    app = FastAPI(title="Hopewell Web UI", version="0.6.0-dev")
    bus = EventBus()
    loop_ref: Dict[str, Any] = {"loop": None}   # set at startup

    static_dir = Path(__file__).parent / "static"

    # ---- static ---------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def _index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # ---- JSON API -------------------------------------------------------
    @app.get("/api/health")
    def _health() -> Dict[str, Any]:
        return {"ok": True, "project": project.cfg.name, "root": str(project.root)}

    @app.get("/api/state")
    def _state() -> Dict[str, Any]:
        # Re-load on every call — cheap on small projects, always fresh.
        p = _load_project(project_root)
        return _state_snapshot(p)

    @app.get("/api/node/{node_id}")
    def _node(node_id: str) -> Dict[str, Any]:
        from hopewell import query as query_mod
        p = _load_project(project_root)
        try:
            return query_mod.show(p, node_id)["node"]
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"node not found: {node_id}")

    @app.get("/api/history")
    def _history(limit: int = 50, cursor: int = 0) -> Dict[str, Any]:
        """Return done nodes reverse-chron with close metadata + deps for tree view.

        Close metadata is derived from `node.close` attestations (ts + actor +
        commit + reason). Nodes that are `done` but have no close attestation
        (e.g. legacy data) fall back to the node's `updated` timestamp and
        empty actor/commit.

        Response shape:
            {
              "items":  [ { "id", "title", "components", "priority", "closed_at",
                            "closed_by", "commit", "reason", "blocked_by" }, ... ],
              "nodes":  { id -> { "id", "title", "status", "components",
                                  "blocked_by", "closed_at" } }   # for tree lookup
              "total":  N,
              "limit":  L,
              "cursor": C,
              "next":   C+L or None
            }
        """
        from hopewell import attestation as att_mod
        p = _load_project(project_root)

        # 1. all nodes — build a summary index used by the tree view.
        all_nodes = p.all_nodes()
        summary: Dict[str, Dict[str, Any]] = {}
        for n in all_nodes:
            s = n.status.value if hasattr(n.status, "value") else n.status
            summary[n.id] = {
                "id": n.id,
                "title": n.title,
                "status": s,
                "components": list(n.components),
                "blocked_by": list(n.blocked_by),
            }

        # 2. scrape node.close attestations — latest wins per node.
        close_meta: Dict[str, Dict[str, Any]] = {}
        try:
            for att in att_mod.iter_attestations(p.attestations_path):
                if att.get("kind") != "node.close":
                    continue
                nid = att.get("node")
                if not nid:
                    continue
                # keep the latest ts
                prev = close_meta.get(nid)
                ts = att.get("ts") or ""
                if prev is None or ts > (prev.get("ts") or ""):
                    close_meta[nid] = {
                        "ts": ts,
                        "actor": att.get("actor"),
                        "commit": att.get("commit"),
                        "reason": att.get("reason"),
                    }
        except Exception:
            # attestations.jsonl absent is fine — fall through to updated-ts fallback.
            pass

        # 3. done nodes sorted by close ts desc.
        done_nodes = [n for n in all_nodes
                      if (n.status.value if hasattr(n.status, "value") else n.status) == "done"]

        def _ts_for(n) -> str:
            meta = close_meta.get(n.id)
            if meta and meta.get("ts"):
                return meta["ts"]
            return n.updated or n.created or ""

        done_nodes.sort(key=_ts_for, reverse=True)
        total = len(done_nodes)

        # Attach closed_at to summary entries so the tree view can style
        # secondary appearances with their own timestamp if wanted.
        for n in done_nodes:
            if n.id in summary:
                summary[n.id]["closed_at"] = _ts_for(n)

        # 4. pagination.
        try:
            cur = max(int(cursor or 0), 0)
        except (TypeError, ValueError):
            cur = 0
        try:
            lim = max(int(limit or 50), 1)
        except (TypeError, ValueError):
            lim = 50
        slice_ = done_nodes[cur:cur + lim]

        items: List[Dict[str, Any]] = []
        for n in slice_:
            meta = close_meta.get(n.id, {})
            items.append({
                "id": n.id,
                "title": n.title,
                "priority": n.priority,
                "components": list(n.components),
                "blocked_by": list(n.blocked_by),
                "closed_at": _ts_for(n),
                "closed_by": meta.get("actor"),
                "commit": meta.get("commit"),
                "reason": meta.get("reason"),
            })

        nxt = cur + lim if cur + lim < total else None
        return {
            "items": items,
            "nodes": summary,
            "total": total,
            "limit": lim,
            "cursor": cur,
            "next": nxt,
        }

    @app.get("/api/waves")
    def _waves() -> Dict[str, Any]:
        from hopewell import query as query_mod
        p = _load_project(project_root)
        return query_mod.waves(p)["stack"]

    @app.get("/api/uat")
    def _uat() -> List[Dict[str, Any]]:
        from hopewell import uat as uat_mod
        p = _load_project(project_root)
        return uat_mod.list_uat(p, status="all")

    @app.post("/api/node/{node_id}/uat-pass")
    def _uat_pass(node_id: str) -> Dict[str, Any]:
        from hopewell import uat as uat_mod
        p = _load_project(project_root)
        try:
            block = uat_mod.mark(p, node_id, "passed",
                                 verified_by="@web-ui", notes="marked via web UI")
            return {"ok": True, "node": node_id, "uat": block}
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"node not found: {node_id}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/node/{node_id}/uat-fail")
    async def _uat_fail(node_id: str, request: Request) -> Dict[str, Any]:
        from hopewell import uat as uat_mod
        p = _load_project(project_root)
        # Optional body: {"reason": "..."} — don't block on malformed JSON.
        reason: Optional[str] = None
        try:
            body = await request.json()
            if isinstance(body, dict):
                reason = body.get("reason")
        except Exception:
            pass
        try:
            block = uat_mod.mark(p, node_id, "failed",
                                 verified_by="@web-ui",
                                 failure_reason=reason or "failed via web UI")
            return {"ok": True, "node": node_id, "uat": block}
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"node not found: {node_id}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/node/{node_id}/uat-waive")
    def _uat_waive(node_id: str) -> Dict[str, Any]:
        from hopewell import uat as uat_mod
        p = _load_project(project_root)
        try:
            block = uat_mod.mark(p, node_id, "waived",
                                 verified_by="@web-ui",
                                 notes="waived via web UI")
            return {"ok": True, "node": node_id, "uat": block}
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"node not found: {node_id}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ---- SSE ------------------------------------------------------------
    @app.get("/api/events")
    async def _events(request: Request):
        async def gen():
            q = await bus.subscribe()
            # initial hello so clients know the stream is alive
            yield b": hopewell-sse hello\n\n"
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield f"data: {payload}\n\n".encode("utf-8")
                    except asyncio.TimeoutError:
                        # keep-alive comment — prevents proxies idling us out
                        yield b": keep-alive\n\n"
            finally:
                await bus.unsubscribe(q)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ---- watchdog -------------------------------------------------------
    @app.on_event("startup")
    async def _startup() -> None:
        loop_ref["loop"] = asyncio.get_event_loop()
        _start_watcher(project_root, bus, loop_ref)

    return app


# ---------------------------------------------------------------------------
# Watchdog wiring
# ---------------------------------------------------------------------------


def _start_watcher(project_root: Path, bus: EventBus, loop_ref: Dict[str, Any]) -> None:
    """Spin up a watchdog observer thread that tails events.jsonl.

    Posting to the bus happens via `asyncio.run_coroutine_threadsafe` so
    the cross-thread handoff into FastAPI's loop stays safe.
    """
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    events_path = (project_root / ".hopewell" / "events.jsonl").resolve()
    if not events_path.parent.is_dir():
        # No .hopewell/ — nothing to tail; still serve the UI for debug.
        return

    def _dispatch(ev: Dict[str, Any]) -> None:
        loop = loop_ref.get("loop")
        if loop is None:
            return
        payload = json.dumps(ev, ensure_ascii=False)
        asyncio.run_coroutine_threadsafe(bus.publish(payload), loop)

    tailer = EventsTailer(events_path, on_event=_dispatch)

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event) -> None:
            if event.is_directory:
                return
            try:
                if Path(event.src_path).resolve() == events_path:
                    tailer.drain_new()
            except OSError:
                pass

        # Treat created/moved as modified — some editors rename-on-save.
        def on_created(self, event) -> None:
            self.on_modified(event)

        def on_moved(self, event) -> None:
            try:
                if Path(getattr(event, "dest_path", "")).resolve() == events_path:
                    tailer.drain_new()
            except OSError:
                pass

    observer = Observer()
    observer.schedule(_Handler(), str(events_path.parent), recursive=False)
    observer.daemon = True
    observer.start()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run(project_root: str = ".", port: int = 7420, open_browser: bool = False,
        host: str = "127.0.0.1") -> None:
    """Start the local web UI.

    Parameters
    ----------
    project_root:
        Path to a project containing `.hopewell/`.
    port:
        TCP port to bind. Default 7420 (HW = H-0x14, W = 0x20 — cute).
    open_browser:
        If True, open http://host:port/ in the system browser once the
        server starts.
    host:
        Bind host. Defaults to 127.0.0.1; override at your own risk.
    """
    _require_web_extras()
    import uvicorn

    root = Path(project_root).resolve()
    app = create_app(root)

    if open_browser:
        # Delay slightly so uvicorn has a chance to bind before we open.
        def _open():
            time.sleep(0.8)
            try:
                webbrowser.open(f"http://{host}:{port}/")
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    sys.stdout.write(f"hopewell web -> http://{host}:{port}/  (project: {root})\n")
    sys.stdout.flush()
    uvicorn.run(app, host=host, port=port, log_level="info")


__all__ = ["run", "create_app"]
