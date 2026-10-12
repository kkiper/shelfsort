"""Scan pipeline: scan -> dedupe -> pages -> identify -> classify -> groups -> plan.

Nothing here touches the files on disk except reading them; Apply does that.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from shelfsort import pdftext
from shelfsort.apply import best_identity
from shelfsort.classify import classify_kind, classify_subject
from shelfsort.dedupe import find_exact_duplicates
from shelfsort.groups import GroupInput, build_groups
from shelfsort.identify import identify_file
from shelfsort.models import FileRecord, FileStatus, Identity, Kind
from shelfsort.plan import PlanInput, clean_filename_title, build_plan
from shelfsort.scan import scan_sources
from shelfsort.settings import Settings
from shelfsort.tools import ToolError

Progress = Callable[[str, int, int], None]

_SKIP = (
    FileStatus.IGNORED.value,
    FileStatus.ORGANIZED.value,
    FileStatus.QUARANTINED.value,
)
_IDENTIFY = (FileStatus.NEW.value, FileStatus.RETRY.value, FileStatus.REVIEW.value)
_FILE_REASONS = ("unreadable", "magazine", "unidentified", "subject", "kind")


@dataclass
class PipelineSummary:
    status_counts: dict[str, int] = field(default_factory=dict)
    subject_counts: dict[str, int] = field(default_factory=dict)
    kind_counts: dict[str, int] = field(default_factory=dict)
    review_reasons: dict[str, int] = field(default_factory=dict)


@dataclass
class _Item:
    rec: FileRecord
    filename: str
    ident: Identity | None = None
    kind: Kind | None = None
    subject: str | None = None
    reason: str | None = None  # review reason, None when the file can be planned


def _record(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=row["id"], source=row["source"], relpath=row["relpath"],
        size=row["size"], mtime=row["mtime"], sha256=row["sha256"],
        ext=row["ext"], pages=row["pages"], status=FileStatus(row["status"]),
    )


def _set_status(conn: sqlite3.Connection, file_id: int, status: FileStatus) -> None:
    conn.execute("update files set status = ? where id = ?", (status.value, file_id))


def _counts(rows) -> dict[str, int]:
    return {k: n for k, n in rows if n}


def _abspath(s: Settings, rec: FileRecord) -> str:
    return os.path.join(s.sources[rec.source], *rec.relpath.split("/"))


def _reason(item: _Item, threshold: float) -> str | None:
    """The single review reason for a file, or None when it can be planned."""
    ident, kind, subject = item.ident, item.kind, item.subject
    if item.rec.status is FileStatus.UNREADABLE:
        return "unreadable"
    below = ident is None or ident.confidence < threshold
    if below and kind is Kind.PLAN and subject is not None:
        return None  # accepted from filename / page count
    if below:
        if ident is not None and ident.magazine:
            return "magazine"
        if ident is None or kind is None or subject is not None:
            return "unidentified"
        return "subject"
    if subject is None:
        return "subject"
    if kind is None:
        return "kind"
    return None


def _load(
    conn: sqlite3.Connection, s: Settings
) -> tuple[list[_Item], list[sqlite3.Row]]:
    """Live items (to identify/classify/plan) and exact-duplicate rows."""
    dups: set[int] = set()
    for _keeper, members in find_exact_duplicates(conn, vague_folders=s.vague_folders):
        dups.update(members)
    marks = ",".join("?" * len(_SKIP))
    rows = conn.execute(
        f"select * from files where status not in ({marks}) order by id", _SKIP
    ).fetchall()
    rows = [r for r in rows if r["source"] in s.sources]
    dup_rows = [r for r in rows if r["id"] in dups]
    live = [_Item(_record(r), os.path.basename(r["relpath"]))
            for r in rows if r["id"] not in dups]
    return live, dup_rows


def run_scan_pipeline(
    conn: sqlite3.Connection, s: Settings, progress: Progress
) -> PipelineSummary:
    # --- scan
    scan_sources(conn, s.sources, lambda n: progress("scan", n, 0))

    # --- exact duplicates
    progress("dedupe", 0, 1)
    live, dup_rows = _load(conn, s)
    progress("dedupe", 1, 1)

    # --- page counts
    pdfs = [it for it in live if it.rec.ext == "pdf" and it.rec.pages is None]
    for i, it in enumerate(pdfs, 1):
        try:
            it.rec.pages = pdftext.page_count(_abspath(s, it.rec))
            conn.execute("update files set pages = ? where id = ?",
                         (it.rec.pages, it.rec.id))
        except (ToolError, OSError):
            it.rec.status = FileStatus.UNREADABLE
            _set_status(conn, it.rec.id, FileStatus.UNREADABLE)
        progress("pages", i, len(pdfs))
    conn.commit()

    # --- identify
    manual = {r[0] for r in conn.execute(
        "select file_id from identities where method = 'manual'")}
    todo = [it for it in live
            if it.rec.status.value in _IDENTIFY and it.rec.id not in manual]
    for i, it in enumerate(todo, 1):
        res = identify_file(conn, it.rec, _abspath(s, it.rec), s)
        it.rec.status = res.status
        _set_status(conn, it.rec.id, res.status)
        progress("identify", i, len(todo))
    conn.commit()
    return _plan_stages(conn, s, live, dup_rows, progress)


def replan(
    conn: sqlite3.Connection, s: Settings, sources: dict[str, str] | None = None
) -> PipelineSummary:
    """Re-run classify, groups, plan and summary only (after review decisions)."""
    if sources is not None:
        s = Settings(**{**vars(s), "sources": dict(sources)})
    live, dup_rows = _load(conn, s)
    return _plan_stages(conn, s, live, dup_rows, lambda *_: None)


def _overrides(conn: sqlite3.Connection) -> dict[int, tuple[str | None, str | None]]:
    """Subject/kind chosen on resolved single-file review items."""
    marks = ",".join("?" * len(_FILE_REASONS))
    out: dict[int, tuple[str | None, str | None]] = {}
    for r in conn.execute(
        "select file_id, decision_json from review_items where resolved = 1"
        f" and file_id is not null and decision_json is not null"
        f" and reason in ({marks}) order by id", _FILE_REASONS):
        try:
            d = json.loads(r["decision_json"])
        except ValueError:
            continue
        if isinstance(d, dict) and (d.get("subject") or d.get("kind")):
            out[r["file_id"]] = (d.get("subject") or None, d.get("kind") or None)
    return out


def _plan_stages(
    conn: sqlite3.Connection, s: Settings, live: list[_Item],
    dup_rows: list[sqlite3.Row], progress: Progress,
) -> PipelineSummary:
    overrides = _overrides(conn)

    # --- classify
    marks = ",".join("?" * len(_FILE_REASONS))
    resolved_ids = {r[0] for r in conn.execute(
        "select file_id from review_items where resolved = 1 and file_id is not null"
        f" and reason in ({marks})", _FILE_REASONS)}
    conn.execute(
        f"delete from review_items where resolved = 0 and reason in ({marks})",
        _FILE_REASONS)
    for i, it in enumerate(live, 1):
        rec = it.rec
        if rec.status is not FileStatus.UNREADABLE:
            it.ident = best_identity(conn, rec.id)
            it.subject = classify_subject(rec.relpath, it.ident, it.filename, s)
            it.kind = classify_kind(it.ident, rec.ext, rec.pages, s)
            subj_over, kind_over = overrides.get(rec.id, (None, None))
            if subj_over:
                it.subject = subj_over
            if kind_over:
                try:
                    it.kind = Kind(kind_over)
                except ValueError:
                    pass
            below = it.ident is None or it.ident.confidence < s.confidence
            if below and it.kind is Kind.PLAN and it.subject is not None:
                if it.ident is None:
                    it.ident = Identity(
                        title=clean_filename_title(it.filename),
                        method="filename", confidence=0.8)
                    conn.execute(
                        "delete from identities where file_id = ? and method = 'filename'",
                        (rec.id,))
                    conn.execute(
                        "insert into identities(file_id, method, json, confidence)"
                        " values (?, ?, ?, ?)",
                        (rec.id, "filename", json.dumps(asdict(it.ident)),
                         it.ident.confidence))
                rec.status = FileStatus.IDENTIFIED
                _set_status(conn, rec.id, rec.status)
        it.reason = _reason(it, s.confidence)
        if it.reason is not None and rec.id not in resolved_ids:
            conn.execute(
                "insert into review_items(reason, file_id, resolved) values (?, ?, 0)",
                (it.reason, rec.id))
            if rec.status is not FileStatus.UNREADABLE:
                rec.status = FileStatus.REVIEW
                _set_status(conn, rec.id, rec.status)
        progress("classify", i, len(live))
    conn.commit()

    # --- groups
    grouped = [
        GroupInput(it.rec.id, it.rec.ext, it.rec.sha256, it.subject, it.ident,
                   it.filename)
        for it in live
        if it.rec.status is not FileStatus.UNREADABLE and it.rec.sha256
    ]
    progress("groups", 0, len(grouped))
    build_groups(conn, grouped, s.fuzzy_threshold)
    progress("groups", len(grouped), len(grouped))

    # --- plan
    held = {r[0] for r in conn.execute(
        "select file_id from review_items where resolved = 0 and file_id is not null"
        " and reason != 'collision'")}  # build_plan owns collision items
    held |= {r[0] for r in conn.execute(
        "select gm.file_id from group_members gm join review_items ri"
        " on ri.group_id = gm.group_id where ri.resolved = 0")}
    quarantined = {r[0] for r in conn.execute(
        "select gm.file_id from group_members gm join review_items ri"
        " on ri.group_id = gm.group_id where ri.resolved = 1"
        " and gm.decision = 'quarantine'")}
    decisions: list[PlanInput] = []
    for it in live:
        r = it.rec
        if r.id in held:
            if r.status is not FileStatus.REVIEW and r.status is not FileStatus.UNREADABLE:
                _set_status(conn, r.id, FileStatus.REVIEW)
            continue
        if r.id in quarantined:
            decisions.append(PlanInput(r.id, r.source, r.relpath, r.ext, None,  # type: ignore[arg-type]
                                       None, None, True))  # type: ignore[arg-type]
            continue
        if it.kind is None or it.subject is None or r.status is FileStatus.UNREADABLE:
            continue
        decisions.append(PlanInput(r.id, r.source, r.relpath, r.ext, it.kind,
                                   it.subject, it.ident, False))
    planned_ids = {d.file_id for d in decisions}
    for r in dup_rows:
        decisions.append(PlanInput(r["id"], r["source"], r["relpath"], r["ext"],
                                   None, None, None, True))  # type: ignore[arg-type]
    progress("plan", 0, len(decisions))
    build_plan(conn, decisions, s.sources)
    conn.executemany(
        "delete from plan_entries where file_id = ?",
        [(it.rec.id,) for it in live if it.rec.id not in planned_ids],
    )
    for d in decisions:
        _set_status(conn, d.file_id, FileStatus.PLANNED)
    conn.commit()
    progress("plan", len(decisions), len(decisions))

    # --- summary
    planned = [d for d in decisions if not d.quarantine]
    return PipelineSummary(
        status_counts=_counts(conn.execute(
            "select status, count(*) from files group by status")),
        subject_counts=dict(Counter(d.subject for d in planned)),
        kind_counts=dict(Counter(d.kind.value for d in planned)),
        review_reasons=_counts(conn.execute(
            "select reason, count(*) from review_items where resolved = 0"
            " group by reason")),
    )
