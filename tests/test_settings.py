from shelfsort import settings


def test_settings_defaults_and_roundtrip(conn):
    s = settings.load(conn)
    assert (s.port, s.confidence, s.plan_page_limit, s.fuzzy_threshold) == (8585, 0.8, 40, 90)
    assert s.sidecar_all is True and s.ollama_enabled is False
    assert s.ollama_model == "llava" and "downloads" in s.vague_folders
    s.magazines = ["American Woodworker"]
    settings.save(conn, s)
    assert settings.load(conn).magazines == ["American Woodworker"]
