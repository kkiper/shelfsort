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

from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
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


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _host_port(netloc: str, scheme: str) -> tuple[str, int] | None:
    try:
        parts = urlsplit(f"//{netloc}")
        port = parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        return None
    if not parts.hostname or port is None:
        return None
    return parts.hostname.lower(), port


def cross_site(request: Request) -> bool:
    """True when a POST's Origin (or, without one, its Referer) is another host:port.

    Requests with neither header (curl, old browsers) pass; the UI is LAN-only.
    """
    host = _host_port(request.headers.get("host", ""), request.url.scheme)
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value is None:
            continue
        try:
            parts = urlsplit(value)
        except ValueError:
            return True
        other = _host_port(parts.netloc, parts.scheme) if parts.netloc else None
        return other is None or other != host
    return False


def create_app(db_path: str, sources_root: str = "/sources") -> FastAPI:
    from shelfsort.web import (
        routes_dashboard, routes_files, routes_history, routes_plan, routes_review,
        routes_settings,
    )

    app = FastAPI(title="shelfsort")
    app.state.db_path = db_path
    app.state.sources_root = sources_root
    app.state.worker = Worker(db_path)
    app.state.templates = Jinja2Templates(directory=str(_HERE / "templates"))

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        if request.method == "POST" and cross_site(request):
            return PlainTextResponse("cross-site request refused", status_code=403)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
    app.include_router(routes_dashboard.router)
    app.include_router(routes_settings.router)
    for module in (routes_review, routes_files, routes_plan, routes_history):
        app.include_router(module.router)

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
