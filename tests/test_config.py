"""Configuration: precedence, secret handling, and the checks that stop a run."""
from __future__ import annotations

from pathlib import Path

import pytest

from kobokit.config import Config, ConfigError


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "kobokit.toml"
    p.write_text(body)
    return p


def test_defaults_need_no_file():
    cfg = Config.load()
    assert cfg.ai.provider == "ollama"
    assert cfg.ai.resolved_model() == "llama3.1:latest"
    assert "ingest" in cfg.run.stages


def test_file_values_win_over_defaults(tmp_path):
    cfg = Config.load(write(tmp_path, """
[ai]
provider = "anthropic"
model = "claude-x"
[run]
workers = 7
"""))
    assert cfg.ai.provider == "anthropic"
    assert cfg.ai.resolved_model() == "claude-x"
    assert cfg.run.workers == 7


def test_env_beats_file(tmp_path, monkeypatch):
    p = write(tmp_path, '[ai]\nprovider = "ollama"\n')
    monkeypatch.setenv("KOBOKIT_AI_PROVIDER", "openai")
    assert Config.load(p).ai.provider == "openai"


def test_cli_override_beats_env(tmp_path, monkeypatch):
    p = write(tmp_path, '[ai]\nprovider = "ollama"\n')
    monkeypatch.setenv("KOBOKIT_AI_PROVIDER", "openai")
    cfg = Config.load(p, {"ai.provider": "anthropic"})
    assert cfg.ai.provider == "anthropic"


def test_api_key_in_the_file_is_refused(tmp_path):
    """A key in a config file is a key in someone's git history."""
    p = write(tmp_path, '[ai]\nprovider = "openai"\napi_key = "sk-realkey"\n')
    with pytest.raises(ConfigError, match="environment"):
        Config.load(p)


def test_key_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    cfg = Config.load(None, {"ai.provider": "anthropic"})
    assert cfg.ai.api_key() == "sk-test"


def test_unknown_setting_is_named(tmp_path):
    p = write(tmp_path, '[recaps]\nenabld = true\n')
    with pytest.raises(ConfigError, match="enabld"):
        Config.load(p)


def test_missing_key_is_reported_when_ai_is_needed(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config.load(None, {
        "ai.provider": "openai",
        "source": tmp_path, "output": tmp_path / "out"})
    problems = cfg.check()
    assert any("OPENAI_API_KEY" in p for p in problems)


def test_no_key_needed_when_recaps_are_off(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config.load(None, {
        "ai.provider": "openai",
        "recaps.enabled": False,
        "metadata.use_ai_fallback": False,
        "source": tmp_path, "output": tmp_path / "out",
        "run.stages": ("ingest", "repair")})
    assert not any("OPENAI_API_KEY" in p for p in cfg.check())


def test_output_may_not_be_the_source(tmp_path):
    cfg = Config.load(None, {"source": tmp_path, "output": tmp_path})
    assert any("same directory" in p for p in cfg.check())


def test_work_and_backup_default_under_output(tmp_path):
    cfg = Config.load(None, {"source": tmp_path, "output": tmp_path / "lib"})
    assert cfg.paths.work.parent == cfg.paths.output
    assert cfg.paths.backup.parent == cfg.paths.output


def test_legacy_recap_env_still_works(monkeypatch):
    monkeypatch.setenv("RECAP_ENDPOINT", "http://box:11434/api/generate")
    monkeypatch.setenv("RECAP_MODEL", "mistral")
    cfg = Config.load()
    # the /api/generate suffix is stripped: the client appends its own path
    assert cfg.ai.resolved_endpoint() == "http://box:11434"
    assert cfg.ai.resolved_model() == "mistral"


def test_missing_config_file_is_an_error_not_a_silent_default():
    """Passing -c with a typo should say so, not quietly run on defaults."""
    with pytest.raises(ConfigError, match="no config file"):
        Config.load(Path("/nonexistent/kobokit.toml"))
