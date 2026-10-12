"""FastAPI application factory.

`create_app(db_path, sources_root)` builds the app. The module-level `app`
(for `uvicorn shelfsort.web.app:app`) is built lazily on first attribute
access via module `__getattr__`, from SHELFSORT_DB (default /config/shelfsort.db)
and SHELFSORT_SOURCES_ROOT (default /sources), so importing this module never
touches /config.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from shelfsort import apply, db
from shelfsort import settings as settings_mod
from shelfsort.jobs import Worker
from shelfsort.settings import Settings

_HERE = Path(__file__).parent


def effective_sources(s: Settings, sources_root: str) -> dict[str, str]:
    """Configured sources, or every immediate subdirectory of `sources_root`."""
    if s.sources:
        return dict(s.sources)
    try:
        entries = sorted(os.scandir(sources_root), key=lambda e: e.name)
    except OSError:
        return {}
    return {e.name: e.path for e in entries if e.is_dir()}


def create_app(db_path: str, sources_root: str = "/sources") -> FastAPI:
    from shelfsort.web import routes_dashboard, routes_settings

    app = FastAPI(title="shelfsort")
    app.state.db_path = db_path
    app.state.sources_root = sources_root
    app.state.worker = Worker(db_path)
    app.state.templates = Jinja2Templates(directory=str(_HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
    app.include_router(routes_dashboard.router)
    app.include_router(routes_settings.router)

    conn = db.connect(db_path)
    try:
        apply.recover(conn, effective_sources(settings_mod.load(conn), sources_root))
    finally:
        conn.close()
    return app


_app: FastAPI | None = None


def __getattr__(name: str):
    global _app
    if name == "app":
        if _app is None:
            _app = create_app(
                os.environ.get("SHELFSORT_DB", "/config/shelfsort.db"),
                os.environ.get("SHELFSORT_SOURCES_ROOT", "/sources"),
            )
        return _app
    raise AttributeError(name)
