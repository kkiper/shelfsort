from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Kind(str, Enum):
    BOOK = "Books"
    MAGAZINE = "Magazines"
    PLAN = "Plans & Articles"


class FileStatus(str, Enum):
    NEW = "new"
    IGNORED = "ignored"
    IDENTIFIED = "identified"
    REVIEW = "review"
    PLANNED = "planned"
    ORGANIZED = "organized"
    QUARANTINED = "quarantined"
    UNREADABLE = "unreadable"
    RETRY = "retry"


@dataclass
class FileRecord:
    id: int | None
    source: str
    relpath: str
    size: int
    mtime: float
    sha256: str | None
    ext: str
    pages: int | None
    status: FileStatus


@dataclass
class Identity:
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    publisher: str | None = None
    isbn: str | None = None
    magazine: str | None = None
    issue_date: str | None = None  # "YYYY-MM"
    issue_number: int | None = None
    cover_story: str | None = None
    method: str = ""
    confidence: float = 0.0


@dataclass
class PlanEntry:
    file_id: int
    action: str  # move | quarantine | rename
    target_relpath: str
    write_meta: bool
    kind: Kind | None
    subject: str | None
