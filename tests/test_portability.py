"""Path helpers must give the right folders with Windows-style and POSIX-style environments."""
from pathlib import Path

import pytest

from recall import context_gauge, paths, session_reader

VARS = ("RECALL_LOCAL_DIR", "RECALL_DEVIN_DIR", "RECALL_DEVIN_CONFIG", "LOCALAPPDATA", "APPDATA",
        "XDG_DATA_HOME", "XDG_CONFIG_HOME")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for v in VARS:
        monkeypatch.delenv(v, raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_windows_style_env(clean_env, monkeypatch, tmp_path):
    local, roaming = tmp_path / "Local", tmp_path / "Roaming"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    (roaming / "devin").mkdir(parents=True)
    (roaming / "devin" / "config.json").write_text("{}", encoding="utf-8")
    assert paths.local_base() == local / "devin-recall"
    assert session_reader.default_db_path() == roaming / "devin" / "cli" / "sessions.db"
    assert context_gauge.devin_config_path() == roaming / "devin" / "config.json"


def test_posix_style_env(clean_env):
    home = clean_env
    assert paths.local_base() == home / ".local" / "share" / "devin-recall"
    assert session_reader.default_db_path() == home / ".config" / "devin" / "cli" / "sessions.db"
    assert context_gauge.devin_config_path() == home / ".config" / "devin" / "config.json"


def test_overrides_win(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("RECALL_LOCAL_DIR", str(tmp_path / "L"))
    monkeypatch.setenv("RECALL_DEVIN_DIR", str(tmp_path / "D"))
    monkeypatch.setenv("RECALL_DEVIN_CONFIG", str(tmp_path / "c.json"))
    assert paths.local_base() == tmp_path / "L"
    assert session_reader.default_db_path() == tmp_path / "D" / "devin" / "cli" / "sessions.db"
    assert context_gauge.devin_config_path() == tmp_path / "c.json"


def test_xdg_env(clean_env, monkeypatch, tmp_path):
    data, conf = tmp_path / "xdata", tmp_path / "xconf"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(conf))
    assert paths.local_base() == data / "devin-recall"
    assert session_reader.default_db_path() == conf / "devin" / "cli" / "sessions.db"
    (conf / "devin").mkdir(parents=True)
    (conf / "devin" / "config.json").write_text("{}", encoding="utf-8")
    assert context_gauge.devin_config_path() == conf / "devin" / "config.json"


def test_macos_candidate_used_when_it_exists(clean_env):
    mac = clean_env / "Library" / "Application Support"
    (mac / "devin").mkdir(parents=True)
    (mac / "devin" / "config.json").write_text("{}", encoding="utf-8")
    assert session_reader.default_db_path() == mac / "devin" / "cli" / "sessions.db"
    assert context_gauge.devin_config_path() == mac / "devin" / "config.json"


def test_local_dir_case_folding_only_on_windows(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("RECALL_LOCAL_DIR", str(tmp_path / "L"))
    # Patch the module flag, not os.name (patching os.name breaks pathlib on the host).
    monkeypatch.setattr(paths, "_WINDOWS", False)
    a = paths.local_dir(tmp_path / "Repo")
    b = paths.local_dir(tmp_path / "repo")
    assert a != b
    monkeypatch.setattr(paths, "_WINDOWS", True)
    assert paths.local_dir(tmp_path / "Repo") == paths.local_dir(tmp_path / "repo")
