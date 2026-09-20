"""`epubkit config get/set/list` - change one setting without an editor.

Writes back by patching the line in place, so the comments explaining what
each setting does survive. A config file that loses its comments the first
time it is edited teaches nobody anything.
"""
from __future__ import annotations

import re
from dataclasses import fields, is_dataclass
from pathlib import Path

from .config import CONFIG_NAME, Config, ConfigError

#: settings that must never be written to a file on disk
FORBIDDEN = re.compile(r"(api[_-]?key|secret|token|password)", re.I)


def sections(cfg: Config) -> dict:
    return {name: getattr(cfg, name) for name in
            ("ai", "metadata", "convert", "recaps", "validate", "run")
            if is_dataclass(getattr(cfg, name))}


def known_keys(cfg: Config) -> list[str]:
    out = ["paths.source", "paths.output", "paths.work", "paths.backup"]
    for name, obj in sections(cfg).items():
        out += ["%s.%s" % (name, f.name) for f in fields(obj)]
    return out


def resolve(cfg: Config, key: str):
    """Current value of a dotted key, raising ConfigError if it is not one."""
    if "." not in key:
        raise ConfigError("%r is not a setting; they look like ai.model" % key)
    section, _, field_name = key.partition(".")
    holder = getattr(cfg, section, None)
    if holder is None or not is_dataclass(holder):
        raise ConfigError("no such section %r. Sections: %s"
                          % (section, ", ".join(["paths", *sections(cfg)])))
    if field_name not in {f.name for f in fields(holder)}:
        near = [k for k in known_keys(cfg) if k.endswith("." + field_name)]
        hint = (" Did you mean %s?" % near[0]) if near else ""
        raise ConfigError("no such setting %r.%s" % (key, hint))
    return getattr(holder, field_name)


def coerce(current, raw: str):
    """Turn a typed-in string into the type the setting already holds."""
    if isinstance(current, bool):
        if raw.lower() in ("true", "yes", "on", "1"):
            return True
        if raw.lower() in ("false", "no", "off", "0"):
            return False
        raise ConfigError("%r is not true or false" % raw)
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(raw)
        except ValueError:
            raise ConfigError("%r is not a whole number" % raw) from None
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError:
            raise ConfigError("%r is not a number" % raw) from None
    if isinstance(current, (tuple, list)):
        return tuple(x.strip() for x in raw.split(",") if x.strip())
    return raw


def render_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (tuple, list)):
        return "[%s]" % ", ".join('"%s"' % v for v in value)
    return '"%s"' % value


def patch(text: str, section: str, key: str, value) -> str:
    """Set section.key in TOML text, keeping comments and layout."""
    rendered = render_value(value)
    lines = text.splitlines()
    header = re.compile(r"^\s*\[%s\]\s*$" % re.escape(section))
    any_header = re.compile(r"^\s*\[[^\]]+\]\s*$")
    assign = re.compile(r"^(\s*)#?\s*%s(\s*)=([^#]*)(#.*)?$"
                        % re.escape(key))

    start = next((i for i, ln in enumerate(lines) if header.match(ln)), None)
    if start is None:
        blank = "" if not lines or lines[-1].strip() == "" else "\n"
        return text.rstrip("\n") + "\n%s\n[%s]\n%s = %s\n" % (
            blank, section, key, rendered)

    for i in range(start + 1, len(lines)):
        if any_header.match(lines[i]):
            break
        m = assign.match(lines[i])
        if m:
            indent, spacing, _, comment = m.groups()
            lines[i] = "%s%s%s= %s%s" % (indent, key, spacing or " ", rendered,
                                         ("   " + comment) if comment else "")
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    else:
        i = len(lines)
    lines.insert(i, "%s = %s" % (key, rendered))
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def target_file(cfg: Config, explicit: Path | None) -> Path:
    if explicit:
        return explicit
    if cfg.source_file:
        return Path(cfg.source_file)
    return Path.cwd() / CONFIG_NAME


def set_value(cfg: Config, key: str, raw: str, path: Path) -> str:
    if FORBIDDEN.search(key):
        raise ConfigError(
            "%s is not kept in the config file. Export it in your shell "
            "instead - a config file gets committed and an environment "
            "variable does not." % key)
    current = resolve(cfg, key)
    value = coerce(current, raw)
    section, _, field_name = key.partition(".")
    text = path.read_text() if path.exists() else ""
    if not text.strip():
        from .cli import EXAMPLE_CONFIG
        text = EXAMPLE_CONFIG
    path.write_text(patch(text, section, field_name, value))
    return "%s = %s" % (key, render_value(value))
