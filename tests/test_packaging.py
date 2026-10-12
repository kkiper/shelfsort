"""Packaged data must resolve from the installed package, not the src layout."""

from importlib import resources

import shelfsort.web.app as app_mod


def test_templates_and_static_resolve_via_importlib_resources():
    web = resources.files("shelfsort.web")
    assert (web / "templates" / "base.html").is_file()
    assert (web / "static" / "app.js").is_file()


def test_app_uses_the_package_resource_directories():
    web = resources.files("shelfsort.web")
    with resources.as_file(web / "templates") as templates:
        assert app_mod._HERE / "templates" == templates
    assert not (app_mod._HERE / "templates").is_symlink()


def test_uvicorn_import_string_resolves_lazy_app(tmp_path, monkeypatch):
    from uvicorn.importer import import_from_string

    monkeypatch.setenv("SHELFSORT_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("SHELFSORT_SOURCES_ROOT", str(tmp_path))
    monkeypatch.setattr(app_mod, "_app", None)
    assert import_from_string("shelfsort.web.app:app").title == "shelfsort"


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def test_docker_healthcheck_uses_healthz():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    health = text[text.index("HEALTHCHECK"):text.index("CMD [\"sh\"")]
    assert "/healthz" in health


def test_unraid_remote_share_uses_slave_propagation():
    import xml.etree.ElementTree as ET

    tree = ET.parse(ROOT / "unraid" / "shelfsort.xml")
    remote = [c for c in tree.iter("Config") if c.get("Target") == "/sources/remote"]
    assert [c.get("Mode") for c in remote] == ["rw,slave"]
