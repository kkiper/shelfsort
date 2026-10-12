"""Dashboard, scan trigger and job progress fragment."""

from __future__ import annotations

import json
from dataclasses import asdict

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from shelfsort import db
from shelfsort import settings as settings_mod
from shelfsort.mounts import check_source
from shelfsort.pipeline import run_scan_pipeline
from shelfsort.web.app import effective_sources

router = APIRouter()


def _last_summary(conn) -> dict | None:
    row = conn.execute("select json from settings where key = 'last_summary'").fetchone()
    return json.loads(row["json"]) if row else None


def _ollama_reachable(s) -> bool:
    try:
        httpx.get(f"{s.ollama_url.rstrip('/')}/api/tags", timeout=2).raise_for_status()
    except Exception:  # bad URL, refused, timeout, non-2xx...: never a 500
        return False
    return True


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    st = request.app.state
    conn = db.connect(st.db_path)
    try:
        s = settings_mod.load(conn)
        summary = _last_summary(conn)
    finally:
        conn.close()
    sources = []
    warnings = []
    for name, path in effective_sources(s, st.sources_root).items():
        status = check_source(path)
        sources.append({"name": name, "path": path, "status": status})
        if not status.mounted:
            warnings.append(f"Source {name} is not mounted: {status.detail}")
    if s.ollama_enabled and not _ollama_reachable(s):
        warnings.append("Ollama unreachable")
    return st.templates.TemplateResponse(request, "dashboard.html", {
        "sources": sources, "summary": summary, "warnings": warnings,
        "job": st.worker.status(),
    })


@router.post("/scan", status_code=202)
def scan(request: Request):
    st = request.app.state

    def job(conn, progress):
        s = settings_mod.load(conn)
        s.sources = effective_sources(s, st.sources_root)
        summary = run_scan_pipeline(conn, s, progress)
        conn.execute(
            "insert into settings(key, json) values ('last_summary', ?) "
            "on conflict(key) do update set json=excluded.json",
            (json.dumps(asdict(summary)),))
        conn.commit()

    if not st.worker.start("scan", job):
        return Response("a job is already running", status_code=409)
    return Response(status_code=202)


@router.get("/job", response_class=HTMLResponse)
def job(request: Request):
    st = request.app.state
    status = st.worker.status()
    resp = st.templates.TemplateResponse(request, "job.html", {"job": status})
    resp.headers["X-Job-Running"] = "true" if status.running else "false"
    return resp
