import os

from shelfsort.mounts import check_source


def test_missing_root_not_mounted(tmp_path):
    st = check_source(str(tmp_path / "nope"))
    assert not st.mounted and not st.writable


def test_file_root_not_mounted(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"x")
    assert not check_source(str(f)).mounted


def test_empty_root_without_marker_not_mounted(tmp_path):
    assert not check_source(str(tmp_path)).mounted
    assert not (tmp_path / ".shelfsort").exists()


def test_nonempty_root_mounts_and_creates_marker(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"x")
    st = check_source(str(tmp_path))
    assert st.mounted and st.writable
    assert (tmp_path / ".shelfsort").is_dir()
    assert not (tmp_path / ".shelfsort" / "write-test").exists()


def test_empty_root_with_marker_is_mounted(tmp_path):
    (tmp_path / ".shelfsort").mkdir()
    assert check_source(str(tmp_path)).mounted


def test_unwritable_marker_reports_not_writable(tmp_path, monkeypatch):
    (tmp_path / "a.pdf").write_bytes(b"x")
    real_open = open

    def deny(path, *a, **k):
        if str(path).endswith("write-test"):
            raise PermissionError("ro")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", deny)
    st = check_source(str(tmp_path))
    assert st.mounted and not st.writable and "ro" in st.detail
