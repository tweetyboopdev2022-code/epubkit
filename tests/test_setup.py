"""Setup: the wizard, `config get/set`, and the dependency preflight."""
from __future__ import annotations

import builtins

import pytest
from conftest import make_epub

from kobokit import configedit as ce
from kobokit import deps, wizard
from kobokit.cli import EXAMPLE_CONFIG
from kobokit.config import Config, ConfigError


def answers(monkeypatch, *replies):
    """Feed the wizard a scripted set of keystrokes."""
    queue = list(replies)

    def fake_input(_prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", fake_input)
    return queue


# -- the wizard ------------------------------------------------------------

def test_wizard_writes_a_config_that_loads(tmp_path, monkeypatch):
    src = tmp_path / "books"
    make_epub(src / "One.epub")
    target = tmp_path / "kobokit.toml"
    monkeypatch.setattr(wizard, "ollama_models",
                        lambda *a, **k: ["qwen3:8b", "llama3.1:latest"])
    answers(monkeypatch,
            str(src),                 # source
            str(tmp_path / "lib"),    # output
            "1",                      # provider: ollama
            "http://box:11434",       # endpoint
            "qwen3:8b",               # model, from the list it fetched
            "y",                      # recaps on
            "y")                      # write it
    assert wizard.run(target) == 0

    cfg = Config.load(target)
    assert cfg.paths.source == src.resolve()
    assert cfg.ai.model == "qwen3:8b"
    assert cfg.ai.resolved_endpoint() == "http://box:11434"
    assert cfg.recaps.enabled is True
    assert cfg.check() == []


def test_wizard_offers_the_models_the_server_really_has(tmp_path, monkeypatch):
    """Typed-from-memory model names are the most common setup mistake."""
    src = tmp_path / "books"
    make_epub(src / "One.epub")
    monkeypatch.setattr(wizard, "ollama_models", lambda *a, **k: ["only-this:8b"])
    answers(monkeypatch, str(src), str(tmp_path / "lib"), "1",
            "http://box:11434", "1", "n", "y")
    wizard.run(tmp_path / "kobokit.toml")
    assert Config.load(tmp_path / "kobokit.toml").ai.model == "only-this:8b"


def test_wizard_rejects_a_source_folder_that_is_not_there(tmp_path, monkeypatch):
    src = tmp_path / "books"
    make_epub(src / "One.epub")
    answers(monkeypatch,
            str(tmp_path / "nope"), "n",      # missing folder, do not use it
            str(src),                          # try again
            str(tmp_path / "lib"), "1", "http://box:11434", "n", "y")
    monkeypatch.setattr(wizard, "ollama_models", lambda *a, **k: [])
    wizard.run(tmp_path / "kobokit.toml")
    assert Config.load(tmp_path / "kobokit.toml").paths.source == src.resolve()


def test_wizard_will_not_let_the_library_be_the_source(tmp_path, monkeypatch):
    src = tmp_path / "books"
    make_epub(src / "One.epub")
    monkeypatch.setattr(wizard, "ollama_models", lambda *a, **k: [])
    answers(monkeypatch,
            str(src),
            str(src),                 # same folder: refused
            str(tmp_path / "lib"),    # and asked again
            "1", "http://box:11434", "n", "y")
    wizard.run(tmp_path / "kobokit.toml")
    cfg = Config.load(tmp_path / "kobokit.toml")
    assert cfg.paths.output != cfg.paths.source


def test_declining_at_the_end_writes_nothing(tmp_path, monkeypatch):
    src = tmp_path / "books"
    make_epub(src / "One.epub")
    monkeypatch.setattr(wizard, "ollama_models", lambda *a, **k: [])
    answers(monkeypatch, str(src), str(tmp_path / "lib"), "1",
            "http://box:11434", "y", "n")
    target = tmp_path / "kobokit.toml"
    assert wizard.run(target) == 1
    assert not target.exists()


def test_ctrl_c_is_not_a_crash(tmp_path, monkeypatch):
    def interrupt(_prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", interrupt)
    with pytest.raises(wizard.Cancelled):
        wizard.run(tmp_path / "kobokit.toml")
    assert not (tmp_path / "kobokit.toml").exists()


# -- config get / set ------------------------------------------------------

def test_set_keeps_the_comments(tmp_path):
    p = tmp_path / "kobokit.toml"
    p.write_text(EXAMPLE_CONFIG)
    cfg = Config.load(p)
    ce.set_value(cfg, "ai.model", "qwen3:8b", p)
    body = p.read_text()
    assert 'model    = "qwen3:8b"' in body
    assert "# ollama | anthropic | openai" in body       # neighbours survive
    assert Config.load(p).ai.model == "qwen3:8b"


def test_set_understands_the_type_of_the_setting(tmp_path):
    p = tmp_path / "kobokit.toml"
    p.write_text(EXAMPLE_CONFIG)
    for key, given, expected in (("run.workers", "4", 4),
                                 ("recaps.enabled", "false", False),
                                 ("recaps.lag_chapters", "2", 2),
                                 ("ai.temperature", "0.5", 0.5)):
        ce.set_value(Config.load(p), key, given, p)
        assert ce.resolve(Config.load(p), key) == expected


def test_set_refuses_a_value_of_the_wrong_type(tmp_path):
    p = tmp_path / "kobokit.toml"
    p.write_text(EXAMPLE_CONFIG)
    with pytest.raises(ConfigError, match="whole number"):
        ce.set_value(Config.load(p), "run.workers", "lots", p)


def test_set_refuses_to_write_a_key(tmp_path):
    p = tmp_path / "kobokit.toml"
    p.write_text(EXAMPLE_CONFIG)
    with pytest.raises(ConfigError, match="not kept in the config file"):
        ce.set_value(Config.load(p), "ai.api_key", "sk-real", p)
    assert "sk-real" not in p.read_text()


def test_a_misspelled_setting_is_named(tmp_path):
    cfg = Config.load()
    with pytest.raises(ConfigError, match="no such setting"):
        ce.resolve(cfg, "ai.modle")
    with pytest.raises(ConfigError, match="no such section"):
        ce.resolve(cfg, "nonsense.thing")


def test_setting_a_section_the_file_lacks_adds_it(tmp_path):
    p = tmp_path / "kobokit.toml"
    p.write_text('[ai]\nprovider = "ollama"\n')
    ce.set_value(Config.load(p), "convert.split_spreads", "always", p)
    assert Config.load(p).convert.split_spreads == "always"
    assert Config.load(p).ai.provider == "ollama"       # and keeps the rest


# -- dependency preflight --------------------------------------------------

def test_every_route_names_libraries_we_know_how_to_install():
    for needed in deps.ROUTES.values():
        for module in needed:
            assert module in deps.OPTIONAL
            assert module in deps.EXTRA_FOR


def test_advice_names_the_module_and_the_command():
    text = deps.advice("numpy")
    assert "numpy" in text and "pip install" in text


def test_install_command_targets_this_interpreter():
    import sys
    cmd = deps.install_command(["numpy", "lxml"])
    assert cmd[0] == sys.executable          # not whatever `pip` is on PATH
    assert "pdf" in cmd[-1] and "full" in cmd[-1]


def test_nothing_missing_means_no_command():
    assert deps.install_command([]) is None
