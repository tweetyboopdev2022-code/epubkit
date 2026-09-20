"""The old name (kobokit) still works, loudly, and never moves anyone's data."""
import json
import logging

import pytest

from epubkit import legacy
from epubkit.config import Config


# conftest stubs Config.discover out so no test picks up the developer's own
# config; these tests are about discovery, so they put the real one back.
_REAL_DISCOVER = Config.__dict__["discover"]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "discover", _REAL_DISCOVER)
    legacy._told.clear()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for k in list(__import__("os").environ):
        if k.startswith(("KOBOKIT_", "EPUBKIT_")):
            monkeypatch.delenv(k)


def test_old_config_file_is_found_with_a_notice(tmp_path, monkeypatch, caplog):
    (tmp_path / "kobokit.toml").write_text('[ai]\nprovider = "ollama"\n')
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING, logger="epubkit"):
        cfg = Config.load()
    assert cfg.ai.provider == "ollama"
    assert "rename it to epubkit.toml" in caplog.text


def test_new_config_file_wins_over_old(tmp_path, monkeypatch):
    (tmp_path / "kobokit.toml").write_text('[ai]\nprovider = "openai"\n')
    (tmp_path / "epubkit.toml").write_text('[ai]\nprovider = "ollama"\n')
    monkeypatch.chdir(tmp_path)
    assert Config.load().ai.provider == "ollama"


def test_old_xdg_config_is_found(tmp_path, monkeypatch):
    d = tmp_path / "xdg" / "kobokit"
    d.mkdir(parents=True)
    (d / "kobokit.toml").write_text('[ai]\nprovider = "openai"\n')
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.chdir(work)
    assert Config.load().ai.provider == "openai"


def test_old_env_var_works_and_new_one_wins(monkeypatch, tmp_path, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KOBOKIT_AI_PROVIDER", "openai")
    with caplog.at_level(logging.WARNING, logger="epubkit"):
        assert Config.load().ai.provider == "openai"
    assert "EPUBKIT_AI_PROVIDER" in caplog.text
    monkeypatch.setenv("EPUBKIT_AI_PROVIDER", "ollama")
    assert Config.load().ai.provider == "ollama"


def test_old_env_path_var_works(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KOBOKIT_PATHS_OUTPUT", str(tmp_path / "lib"))
    assert Config.load().paths.output == (tmp_path / "lib").resolve()


def test_existing_old_work_and_backup_dirs_are_used_in_place(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "lib"
    (out / ".kobokit-work").mkdir(parents=True)
    (out / ".kobokit-originals").mkdir()
    cfg = Config.load(None, {"source": tmp_path, "output": out})
    assert cfg.paths.work == (out / ".kobokit-work").resolve()
    assert cfg.paths.backup == (out / ".kobokit-originals").resolve()
    assert (out / ".kobokit-originals").is_dir()          # not moved or removed


def test_fresh_library_uses_new_dir_names(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "lib"
    cfg = Config.load(None, {"source": tmp_path, "output": out})
    assert cfg.paths.work.name == ".epubkit-work"
    assert cfg.paths.backup.name == ".epubkit-originals"


def test_new_dir_wins_when_both_exist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "lib"
    (out / ".kobokit-work").mkdir(parents=True)
    (out / ".epubkit-work").mkdir()
    cfg = Config.load(None, {"source": tmp_path, "output": out})
    assert cfg.paths.work.name == ".epubkit-work"


def test_old_state_file_is_copied_not_moved(tmp_path):
    (tmp_path / "kobokit-state.json").write_text(json.dumps({"schema": 1}))
    legacy.adopt_state(tmp_path, "epubkit-state.json")
    assert (tmp_path / "epubkit-state.json").read_text() == '{"schema": 1}'
    assert (tmp_path / "kobokit-state.json").exists()


def test_existing_new_state_file_is_never_overwritten(tmp_path):
    (tmp_path / "kobokit-state.json").write_text("old")
    (tmp_path / "epubkit-state.json").write_text("new")
    legacy.adopt_state(tmp_path, "epubkit-state.json")
    assert (tmp_path / "epubkit-state.json").read_text() == "new"
