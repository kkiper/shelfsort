"""Review queue: group decisions, single-item edits, collisions and bulk actions."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from shelfsort import db
from shelfsort import settings as settings_mod
from shelfsort.apply import best_identity
from shelfsort.classify import classify_kind, classify_subject
from shelfsort.models import Identity, Kind
from shelfsort.pipeline import replan
from shelfsort.settings import Settings
from shelfsort.web.app import effective_sources

router = APIRouter()

PAGE_SIZE = 50
TABS: dict[str, tuple[str, ...]] = {
    "variant": ("variant",),
    "near": ("near",),
    "possible": ("possible",),
    "unidentified": ("unidentified", "unreadable"),
    "magazine": ("magazine",),
    "subject": ("subject", "kind"),
    "collision": ("collision",),
}
GROUP_TABS = ("variant", "near", "possible")
_GROUP_REASONS = frozenset(GROUP_TABS)
_COVER_EXTS = frozenset({"pdf", "jpg", "jpeg", "png"})
_DECISION_KEY = re.compile(r"^decisions\[(\d+)\]$")
_ISSUE_DATE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_KINDS = [k.value for k in Kind]


class BadRequest(ValueError):
    pass


def tab_of(reason: str) -> str:
    return next(t for t, rs in TABS.items() if reason in rs)


def error_page(request: Request, message: str, status: int) -> Response:
    return request.app.state.templates.TemplateResponse(
        request, "error.html", {"message": message}, status_code=status)


def load_settings(conn: sqlite3.Connection, request: Request) -> Settings:
    st = request.app.state
    s = settings_mod.load(conn)
    s.sources = effective_sources(s, st.sources_root)
    return s


def busy_response(request: Request) -> Response | None:
    """409 while a background job runs (it would race with the change)."""
    if request.app.state.worker.status().running:
        return error_page(request, "a job is running; try again when it has finished", 409)
    return None


def _marks(values) -> str:
    return ",".join("?" * len(values))


def _file_view(conn: sqlite3.Connection, s: Settings, file_id: int) -> dict:
    f = conn.execute("select * from files where id = ?", (file_id,)).fetchone()
    ident = best_identity(conn, file_id)
    filename = f["relpath"].rpartition("/")[2]
    subject = classify_subject(f["relpath"], ident, filename, s)
    kind = classify_kind(ident, f["ext"], f["pages"], s)
    return {
        "id": f["id"], "source": f["source"], "relpath": f["relpath"],
        "ext": f["ext"], "size": f["size"], "pages": f["pages"], "status": f["status"],
        "ident": ident or Identity(), "has_ident": ident is not None,
        "authors": ", ".join(ident.authors) if ident else "",
        "subject": subject or "", "kind": kind.value if kind else "",
        "cover": f["ext"] in _COVER_EXTS,
    }


def _card(conn: sqlite3.Connection, s: Settings, item: sqlite3.Row) -> dict:
    card = {"id": item["id"], "reason": item["reason"], "resolved": bool(item["resolved"]),
            "tab": tab_of(item["reason"]), "kinds": _KINDS}
    if item["group_id"] is not None:
        members = conn.execute(
            "select file_id, decision from group_members where group_id = ? order by file_id",
            (item["group_id"],)).fetchall()
        card["members"] = [
            {**_file_view(conn, s, m["file_id"]), "decision": m["decision"]} for m in members]
    else:
        card["file"] = _file_view(conn, s, item["file_id"])
    return card


def _item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("select * from review_items where id = ?", (item_id,)).fetchone()


def _unresolved(conn: sqlite3.Connection, tab: str) -> list[sqlite3.Row]:
    reasons = TABS[tab]
    return conn.execute(
        f"select * from review_items where resolved = 0 and reason in ({_marks(reasons)})"
        " order by id", reasons).fetchall()


def _resolve(conn: sqlite3.Connection, item_id: int, decision: dict) -> None:
    conn.execute("update review_items set resolved = 1, decision_json = ? where id = ?",
                 (json.dumps(decision), item_id))


def _decide_group(conn: sqlite3.Connection, item: sqlite3.Row, decisions: dict[int, str]) -> None:
    conn.executemany(
        "update group_members set decision = ? where group_id = ? and file_id = ?",
        [(d, item["group_id"], fid) for fid, d in decisions.items()])
    _resolve(conn, item["id"], {"decisions": {str(k): v for k, v in decisions.items()}})


def _save_manual(conn: sqlite3.Connection, file_id: int, ident: Identity) -> None:
    ident.method, ident.confidence = "manual", 1.0
    conn.execute("delete from identities where file_id = ? and method = 'manual'", (file_id,))
    conn.execute(
        "insert into identities(file_id, method, json, confidence) values (?, 'manual', ?, 1.0)",
        (file_id, json.dumps(asdict(ident))))


def _text(form, name: str) -> str | None:
    return (form.get(name) or "").strip() or None


def _int(form, name: str) -> int | None:
    raw = _text(form, name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        raise BadRequest(f"{name}: {raw!r} is not a whole number") from None


def parse_identity(form) -> Identity:
    date = _text(form, "issue_date")
    if date is not None and not _ISSUE_DATE.match(date):
        raise BadRequest(f"issue_date: {date!r} must look like YYYY-MM")
    authors = [a.strip() for a in (form.get("authors") or "").split(",") if a.strip()]
    return Identity(
        title=_text(form, "title"), authors=authors, year=_int(form, "year"),
        publisher=_text(form, "publisher"), isbn=_text(form, "isbn"),
        magazine=_text(form, "magazine"), issue_date=date,
        issue_number=_int(form, "issue_number"), cover_story=_text(form, "cover_story"))


def _back(tab: str) -> RedirectResponse:
    return RedirectResponse(f"/review?tab={tab}", status_code=303)


@router.get("/review", response_class=HTMLResponse)
def review(request: Request, tab: str = "variant", page: int = 1):
    if tab not in TABS:
        return error_page(request, f"unknown tab {tab!r}", 400)
    page = max(page, 1)
    conn = db.connect(request.app.state.db_path)
    try:
        s = load_settings(conn, request)
        counts = {t: conn.execute(
            f"select count(*) from review_items where resolved = 0 and reason in ({_marks(rs)})",
            rs).fetchone()[0] for t, rs in TABS.items()}
        reasons = TABS[tab]
        items = conn.execute(
            f"select * from review_items where resolved = 0 and reason in ({_marks(reasons)})"
            " order by id limit ? offset ?",
            (*reasons, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
        cards = [_card(conn, s, i) for i in items]
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(request, "review.html", {
        "tab": tab, "tabs": list(TABS), "counts": counts, "cards": cards, "page": page,
        "has_next": counts[tab] > page * PAGE_SIZE, "group_tab": tab in GROUP_TABS,
    })


@router.get("/review/{item_id}", response_class=HTMLResponse)
def card(request: Request, item_id: int):
    conn = db.connect(request.app.state.db_path)
    try:
        item = _item(conn, item_id)
        if item is None:
            return error_page(request, "no such review item", 404)
        c = _card(conn, load_settings(conn, request), item)
    finally:
        conn.close()
    name = "review_group.html" if "members" in c else "review_single.html"
    return request.app.state.templates.TemplateResponse(request, name, {"card": c})


def _guard(request: Request, conn: sqlite3.Connection, item_id: int, group: bool):
    """Return (item, None) or (None, error response)."""
    busy = busy_response(request)
    if busy is not None:
        return None, busy
    item = _item(conn, item_id)
    if item is None:
        return None, error_page(request, "no such review item", 404)
    if (item["reason"] in _GROUP_REASONS) != group:
        return None, error_page(request, "wrong kind of review item for this action", 400)
    if item["resolved"]:
        return None, error_page(request, "this item is already resolved", 409)
    return item, None


@router.post("/review/bulk")
async def bulk(request: Request):
    form = await request.form()
    tab, action = form.get("tab"), form.get("action")
    fmt = (form.get("format") or "").strip().lower().lstrip(".") or None
    if tab not in TABS:
        return error_page(request, f"unknown tab {tab!r}", 400)
    if action not in ("accept_suggestions", "keep_format"):
        return error_page(request, f"unknown action {action!r}", 400)
    if action == "keep_format" and (tab not in GROUP_TABS or fmt is None):
        return error_page(request, "keep_format needs a group tab and a format", 400)
    busy = busy_response(request)
    if busy is not None:
        return busy
    conn = db.connect(request.app.state.db_path)
    try:
        s = load_settings(conn, request)
        items = _unresolved(conn, tab)
        if tab in GROUP_TABS:
            _bulk_groups(conn, items, action, fmt)
        else:
            _bulk_singles(conn, s, items)
        conn.commit()
        replan(conn, s)
        return _back(tab)
    finally:
        conn.close()


def _bulk_groups(conn, items, action: str, fmt: str | None) -> None:
    for item in items:
        members = conn.execute(
            "select gm.file_id, f.ext from group_members gm join files f on f.id = gm.file_id"
            " where gm.group_id = ?", (item["group_id"],)).fetchall()
        if action == "accept_suggestions":
            decisions = {m["file_id"]: "keep" for m in members}
        elif any(m["ext"] == fmt for m in members):
            decisions = {m["file_id"]: "keep" if m["ext"] == fmt else "quarantine"
                         for m in members}
        else:
            continue  # group without that format stays unresolved
        _decide_group(conn, item, decisions)


def _bulk_singles(conn, s: Settings, items) -> None:
    for item in items:
        if item["reason"] == "collision":
            _resolve(conn, item["id"], {"accepted": True})
            continue
        v = _file_view(conn, s, item["file_id"])
        if not v["has_ident"] or not v["subject"] or not v["kind"]:
            continue  # nothing usable to accept
        _save_manual(conn, item["file_id"], v["ident"])
        _resolve(conn, item["id"], {"subject": v["subject"], "kind": v["kind"]})


@router.post("/review/{item_id}/group")
async def decide_group(request: Request, item_id: int):
    form = await request.form()
    conn = db.connect(request.app.state.db_path)
    try:
        item, err = _guard(request, conn, item_id, True)
        if err is not None:
            return err
        members = {r[0] for r in conn.execute(
            "select file_id from group_members where group_id = ?", (item["group_id"],))}
        decisions = {fid: "keep" for fid in members}  # unspecified members are kept
        for key, value in form.multi_items():
            m = _DECISION_KEY.match(key)
            if m and int(m.group(1)) in members:
                if value not in ("keep", "quarantine"):
                    return error_page(request, f"bad decision {value!r}", 400)
                decisions[int(m.group(1))] = value
        if "keep" not in decisions.values():
            return error_page(request, "at least one file must be kept", 400)
        _decide_group(conn, item, decisions)
        conn.commit()
        replan(conn, load_settings(conn, request))
        return _back(tab_of(item["reason"]))
    finally:
        conn.close()


@router.post("/review/{item_id}/single")
async def decide_single(request: Request, item_id: int):
    form = await request.form()
    conn = db.connect(request.app.state.db_path)
    try:
        item, err = _guard(request, conn, item_id, False)
        if err is not None:
            return err
        if item["reason"] == "collision":
            return error_page(request, "collisions are accepted, not edited", 400)
        kind = _text(form, "kind")
        if kind is not None and kind not in _KINDS:
            return error_page(request, f"kind must be one of {', '.join(_KINDS)}", 400)
        try:
            ident = parse_identity(form)
        except BadRequest as exc:
            return error_page(request, str(exc), 400)
        _save_manual(conn, item["file_id"], ident)
        _resolve(conn, item_id, {"subject": _text(form, "subject"), "kind": kind})
        conn.commit()
        replan(conn, load_settings(conn, request))
        return _back(tab_of(item["reason"]))
    finally:
        conn.close()


@router.post("/review/{item_id}/accept")
def accept_collision(request: Request, item_id: int):
    conn = db.connect(request.app.state.db_path)
    try:
        item, err = _guard(request, conn, item_id, False)
        if err is not None:
            return err
        if item["reason"] != "collision":
            return error_page(request, "only collisions can be accepted this way", 400)
        _resolve(conn, item_id, {"accepted": True})
        conn.commit()
        return _back("collision")
    finally:
        conn.close()
