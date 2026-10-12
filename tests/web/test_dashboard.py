import json

import httpx
import pytest

from shelfsort import db
from shelfsort import settings as settings_mod
from shelfsort.web import app as app_mod


def test_dashboard_shows_unmounted_source(client_factory, tmp_path):
    client = client_factory(sources={"remote": str(tmp_path / "missing")})
    r = client.get("/")
    assert r.status_code == 200 and "not mounted" in r.text
    assert "remote" in r.text


def test_dashboard_shows_mounted_writable_source(client_factory, tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "a.pdf").write_bytes(b"x")
    r = client_factory(sources={"lib": str(root)}).get("/")
    assert "not mounted" not in r.text and "writable" in r.text


def test_sources_auto_discovered_from_root(client_factory, tmp_path):
    root = tmp_path / "sources"
    (root / "alpha").mkdir(parents=True)
    (root / "alpha" / "a.pdf").write_bytes(b"x")
    (root / "beta").mkdir()
    (root / "stray.txt").write_bytes(b"x")
    r = client_factory(sources_root=root).get("/")
    assert "alpha" in r.text and "beta" in r.text and "stray.txt" not in r.text
    assert app_mod.effective_sources(settings_mod.Settings(), str(root)) == {
        "alpha": str(root / "alpha"), "beta": str(root / "beta")}


def test_explicit_sources_override_discovery(tmp_path):
    (tmp_path / "found").mkdir()
    s = settings_mod.Settings(sources={"mine": "/x"})
    assert app_mod.effective_sources(s, str(tmp_path)) == {"mine": "/x"}
    assert app_mod.effective_sources(settings_mod.Settings(), str(tmp_path / "nope")) == {}


def test_scan_rejects_second_job(client, slow_job):
    assert client.post("/scan").status_code == 202
    assert client.post("/scan").status_code == 409
    assert client.get("/job").headers["x-job-running"] == "true"


def test_scan_stores_last_summary_and_dashboard_shows_it(client_factory, tmp_path):
    root = tmp_path / "sources"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "a.pdf").write_bytes(b"%PDF-1.4 x")
    client = client_factory(sources_root=root)
    assert client.post("/scan").status_code == 202
    client.app.state.worker.join(30)
    conn = db.connect(client.db_path)
    row = conn.execute("select json from settings where key='last_summary'").fetchone()
    conn.close()
    assert row is not None and "status_counts" in json.loads(row["json"])
    assert "Last scan" in client.get("/").text
    assert client.get("/job").headers["x-job-running"] == "false"


def test_job_shows_error_after_job_raises(client, monkeypatch):
    from shelfsort.web import routes_dashboard

    def boom(conn, s, progress):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(routes_dashboard, "run_scan_pipeline", boom)
    assert client.post("/scan").status_code == 202
    client.app.state.worker.join(10)
    r = client.get("/job")
    assert r.status_code == 200 and "kaboom" in r.text
    assert client.get("/").status_code == 200  # server still alive
    assert client.post("/scan").status_code == 202  # and can run another job


def test_ollama_unreachable_warning(client_factory, monkeypatch):
    client = client_factory()
    c = db.connect(client.db_path)
    s = settings_mod.load(c)
    s.ollama_enabled, s.ollama_url = True, "http://ollama.invalid:11434"
    settings_mod.save(c, s)
    c.close()

    def fail(url, timeout):
        assert url == "http://ollama.invalid:11434/api/tags" and timeout == 2
        raise httpx.ConnectError("nope")

    monkeypatch.setattr("shelfsort.web.routes_dashboard.httpx.get", fail)
    assert "Ollama unreachable" in client.get("/").text


def test_recover_called_on_startup(client_factory, monkeypatch):
    calls = []
    monkeypatch.setattr(app_mod.apply, "recover", lambda conn, sources: calls.append(sources) or 0)
    client_factory(sources={"remote": "/somewhere"})
    assert calls == [{"remote": "/somewhere"}]


def test_module_app_is_lazy(monkeypatch, tmp_path):
    monkeypatch.setenv("SHELFSORT_DB", str(tmp_path / "lazy.db"))
    monkeypatch.setenv("SHELFSORT_SOURCES_ROOT", str(tmp_path))
    monkeypatch.delitem(app_mod.__dict__, "app", raising=False)
    assert not (tmp_path / "lazy.db").exists()
    assert app_mod.app.title == "shelfsort"
    assert (tmp_path / "lazy.db").exists()


def test_no_route_runs_sync_work_on_the_event_loop():
    """I4: every endpoint is a plain def (FastAPI runs it in the threadpool)."""
    import inspect

    from shelfsort.web import (
        routes_dashboard, routes_files, routes_history, routes_plan, routes_review,
        routes_settings,
    )

    endpoints = [r.endpoint for m in (routes_dashboard, routes_files, routes_history,
                                      routes_plan, routes_review, routes_settings)
                 for r in m.router.routes]
    assert endpoints
    assert [e.__name__ for e in endpoints if inspect.iscoroutinefunction(e)] == []


def test_ollama_check_never_500s(client_factory, monkeypatch):
    client = client_factory()
    c = db.connect(client.db_path)
    s = settings_mod.load(c)
    s.ollama_enabled, s.ollama_url = True, "http://o:11434"
    settings_mod.save(c, s)
    c.close()

    def fail(url, timeout):
        raise httpx.InvalidURL("bad")

    monkeypatch.setattr("shelfsort.web.routes_dashboard.httpx.get", fail)
    r = client.get("/")
    assert r.status_code == 200 and "Ollama unreachable" in r.text


# ------------------------------------------------------------- I5: CSRF

@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},
    {"Origin": "http://testserver:9999"},
    {"Origin": "null"},
    {"Referer": "http://evil.example/history"},
])
def test_cross_site_post_is_403(client, headers):
    r = client.post("/quarantine/empty", data={"source": "x", "confirm": "no"}, headers=headers)
    assert r.status_code == 403


@pytest.mark.parametrize("headers", [
    {},
    {"Origin": "http://testserver"},
    {"Origin": "http://TESTSERVER:80"},
    {"Referer": "http://testserver/history"},
])
def test_same_origin_post_passes(client, headers):
    r = client.post("/quarantine/empty", data={"source": "x", "confirm": "no"}, headers=headers)
    assert r.status_code == 400  # reached the handler (bad confirmation)


def test_cross_site_get_is_not_blocked(client):
    assert client.get("/settings", headers={"Origin": "http://evil.example"}).status_code == 200
