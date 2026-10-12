"""Plan table and the Apply job."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from starlette.datastructures import FormData
from fastapi.responses import HTMLResponse

from shelfsort import db
from shelfsort.apply import create_batch, run_batch
from shelfsort.web.routes_review import form_data, load_settings

router = APIRouter()

PAGE_SIZE = 200
_SCOPE_KEYS = ("source", "subject", "kind")
# Only entries of files still waiting to be applied (not held for review etc.).
_PLANNED = "from plan_entries p join files f on f.id = p.file_id where f.status = 'planned'"


def _scope(data) -> dict[str, str]:
    return {k: v.strip() for k in _SCOPE_KEYS if isinstance(v := data.get(k), str) and v.strip()}


@router.get("/plan", response_class=HTMLResponse)
def plan(request: Request, source: str = "", subject: str = "", kind: str = "", page: int = 1):
    page = max(page, 1)
    scope = _scope({"source": source, "subject": subject, "kind": kind})
    where = " and ".join(["f.status = 'planned'"] + [
        f"{'f.' if k == 'source' else 'p.'}{k} = ?" for k in scope])
    conn = db.connect(request.app.state.db_path)
    try:
        total = conn.execute(
            "select count(*) from plan_entries p join files f on f.id = p.file_id"
            f" where {where}", list(scope.values())).fetchone()[0]
        rows = conn.execute(
            "select f.source, f.relpath, p.action, p.target_relpath, p.kind, p.subject"
            " from plan_entries p join files f on f.id = p.file_id"
            f" where {where} order by f.id limit ? offset ?",
            [*scope.values(), PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
        facets = {
            "sources": [r[0] for r in conn.execute(
                f"select distinct f.source {_PLANNED} order by 1")],
            "subjects": [r[0] for r in conn.execute(
                f"select distinct p.subject {_PLANNED} and p.subject is not null order by 1")],
            "kinds": [r[0] for r in conn.execute(
                f"select distinct p.kind {_PLANNED} and p.kind is not null order by 1")],
        }
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(request, "plan.html", {
        "rows": rows, "total": total, "scope": scope, "facets": facets, "page": page,
        "has_next": total > page * PAGE_SIZE, "job": request.app.state.worker.status(),
    })


@router.post("/apply", status_code=202)
def apply_plan(request: Request, form: FormData = Depends(form_data)):
    scope = _scope(form)
    st = request.app.state

    def job(conn, progress):
        s = load_settings(conn, request)
        progress("apply", 0, 1)
        run_batch(conn, create_batch(conn, scope), s.sources, s)
        progress("apply", 1, 1)

    if not st.worker.start("apply", job):
        return Response("a job is already running", status_code=409)
    return Response(status_code=202)
