import json
import sqlite3
from dataclasses import asdict, dataclass, field, fields

_KEY = "settings"


@dataclass
class Settings:
    port: int = 8585
    confidence: float = 0.8
    plan_page_limit: int = 40
    fuzzy_threshold: int = 90
    isbn_first_pages: int = 10
    isbn_last_pages: int = 5
    sidecar_all: bool = True
    sources: dict[str, str] = field(default_factory=dict)  # name -> container path
    subject_map: dict[str, str] = field(default_factory=dict)
    vague_folders: list[str] = field(default_factory=lambda: [
        "downloads", "misc", "new folder", "unsorted", "temp", "inbox"])
    keyword_rules: dict[str, list[str]] = field(default_factory=dict)
    magazines: list[str] = field(default_factory=list)
    ollama_enabled: bool = False
    ollama_url: str = ""
    ollama_model: str = "llava"


def load(conn: sqlite3.Connection) -> Settings:
    row = conn.execute("select json from settings where key=?", (_KEY,)).fetchone()
    if row is None:
        return Settings()
    known = {f.name for f in fields(Settings)}
    data = {k: v for k, v in json.loads(row["json"]).items() if k in known}
    return Settings(**data)


def save(conn: sqlite3.Connection, s: Settings) -> None:
    conn.execute(
        "insert into settings(key, json) values (?, ?) "
        "on conflict(key) do update set json=excluded.json",
        (_KEY, json.dumps(asdict(s))),
    )
    conn.commit()
