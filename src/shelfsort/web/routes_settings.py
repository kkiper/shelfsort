"""Settings form."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.datastructures import FormData
from fastapi.responses import HTMLResponse

from shelfsort import db
from shelfsort import settings as settings_mod
from shelfsort.settings import Settings
from shelfsort.web.routes_review import form_data

router = APIRouter()


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _pairs(text: str, label: str) -> list[tuple[str, str]]:
    out = []
    for ln in _lines(text):
        key, sep, value = ln.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise ValueError(f"{label}: expected 'key = value', got {ln!r}")
        out.append((key.strip(), value.strip()))
    return out


def _number(form, name: str, current, cast, lo, hi):
    raw = (form.get(name) or "").strip()
    if not raw:
        return current
    try:
        value = cast(raw)
    except ValueError:
        raise ValueError(f"{name}: {raw!r} is not a valid number") from None
    if not lo <= value <= hi:
        raise ValueError(f"{name}: must be between {lo} and {hi}")
    return value


def parse_form(form, current: Settings) -> Settings:
    """Build new Settings from submitted form data; raises ValueError on bad input."""
    s = Settings(**vars(current))
    if "magazines" in form:
        s.magazines = _lines(form["magazines"])
    if "vague_folders" in form:
        s.vague_folders = _lines(form["vague_folders"])
    if "subject_map" in form:
        s.subject_map = dict(_pairs(form["subject_map"], "subject_map"))
    if "keyword_rules" in form:
        s.keyword_rules = {
            subject: [k.strip() for k in kws.split(",") if k.strip()]
            for subject, kws in _pairs(form["keyword_rules"], "keyword_rules")}
    if "sources" in form:
        s.sources = dict(_pairs(form["sources"], "sources"))
    s.confidence = _number(form, "confidence", s.confidence, float, 0.0, 1.0)
    s.plan_page_limit = _number(form, "plan_page_limit", s.plan_page_limit, int, 1, 100000)
    s.fuzzy_threshold = _number(form, "fuzzy_threshold", s.fuzzy_threshold, int, 0, 100)
    s.sidecar_all = "sidecar_all" in form
    s.ollama_enabled = "ollama_enabled" in form
    s.ollama_url = (form.get("ollama_url") or "").strip()
    s.ollama_model = (form.get("ollama_model") or "").strip() or current.ollama_model
    return s


def _view(s: Settings) -> dict:
    return {
        "magazines": "\n".join(s.magazines),
        "vague_folders": "\n".join(s.vague_folders),
        "subject_map": "\n".join(f"{k} = {v}" for k, v in s.subject_map.items()),
        "keyword_rules": "\n".join(f"{k} = {', '.join(v)}" for k, v in s.keyword_rules.items()),
        "sources": "\n".join(f"{k} = {v}" for k, v in s.sources.items()),
        "confidence": s.confidence, "plan_page_limit": s.plan_page_limit,
        "fuzzy_threshold": s.fuzzy_threshold, "sidecar_all": s.sidecar_all,
        "ollama_enabled": s.ollama_enabled, "ollama_url": s.ollama_url,
        "ollama_model": s.ollama_model,
    }


def _render(request: Request, values: dict, error: str | None = None, status: int = 200):
    return request.app.state.templates.TemplateResponse(
        request, "settings.html", {"v": values, "error": error}, status_code=status)


@router.get("/settings", response_class=HTMLResponse)
def show(request: Request):
    conn = db.connect(request.app.state.db_path)
    try:
        return _render(request, _view(settings_mod.load(conn)))
    finally:
        conn.close()


@router.post("/settings", response_class=HTMLResponse)
def save(request: Request, form: FormData = Depends(form_data)):
    conn = db.connect(request.app.state.db_path)
    try:
        current = settings_mod.load(conn)
        try:
            new = parse_form(form, current)
        except ValueError as exc:
            values = _view(current)
            values.update({k: form[k] for k in values if k in form and isinstance(form[k], str)})
            values.update({k: k in form for k in ("sidecar_all", "ollama_enabled")})
            return _render(request, values, str(exc), 400)
        settings_mod.save(conn, new)
        return _render(request, _view(new))
    finally:
        conn.close()
