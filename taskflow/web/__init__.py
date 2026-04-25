"""taskflow web UI — optional, requires the `[web]` extra.

Entry point is `hopewell.web.server.run(project_root, port, open_browser)`.
All heavy dependencies (fastapi, uvicorn, watchdog) are imported lazily
inside server.py so that merely importing this package on a core-only
install remains harmless.
"""
from __future__ import annotations

__all__ = ["run"]


def run(project_root: str = ".", port: int = 7420, open_browser: bool = False,
        host: str = "127.0.0.1") -> None:
    """Lazy re-export of `hopewell.web.server.run`.

    Keeps `import taskflow.web` cheap on non-web installs; only when
    someone actually calls `run` do we pull in FastAPI/uvicorn.
    """
    from taskflow.web.server import run as _run
    _run(project_root=project_root, port=port, open_browser=open_browser, host=host)
