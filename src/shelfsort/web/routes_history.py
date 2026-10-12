"""Batch history, undo and quarantine emptying."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from shelfsort import db
from shelfsort.apply import empty_quarantine, undo_batch
from shelfsort.web.routes_review import busy_response, error_page, load_settings

router = APIRouter()


def _render(request: Request, message: str | None = None):
    conn = db.connect(request.app.state.db_path)
    try:
        batches = [dict(b) for b in conn.execute(
            "select * from batches order by id desc").fetchall()]
        for b in batches:
            b["counts"] = {r[0]: r[1] for r in conn.execute(
                "select state, count(*) from journal where batch_id = ? group by state",
                (b["id"],))}
        sources = list(load_settings(conn, request).sources)
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(request, "history.html", {
        "batches": batches, "sources": sources, "message": message,
        "job": request.app.state.worker.status(),
    })


@router.get("/history", response_class=HTMLResponse)
def history(request: Request):
    return _render(request)


@router.post("/history/{batch_id}/undo", status_code=202)
def undo(request: Request, batch_id: int):
    def job(conn, progress):
        progress("undo", 0, 1)
        undo_batch(conn, batch_id, load_settings(conn, request).sources)
        progress("undo", 1, 1)

    if not request.app.state.worker.start("undo", job):
        return Response("a job is already running", status_code=409)
    return Response(status_code=202)


@router.post("/quarantine/empty")
async def empty(request: Request):
    form = await request.form()
    busy = busy_response(request)
    if busy is not None:
        return busy
    conn = db.connect(request.app.state.db_path)
    try:
        sources = load_settings(conn, request).sources
        try:
            n = empty_quarantine(
                conn, sources, str(form.get("source") or ""), str(form.get("confirm") or ""),
                include_undone="include_undone" in form)
        except ValueError as exc:  # includes SourceUnavailable
            return error_page(request, str(exc), 400)
    finally:
        conn.close()
    return _render(request, f"Quarantine emptied: {n} file(s) deleted.")
