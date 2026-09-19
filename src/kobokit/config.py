"""Configuration: a TOML file, environment variables, and CLI flags.

Precedence, lowest to highest: built-in defaults, the config file, environment
variables, then command-line flags. That order exists so a checked-in config
can describe the shape of a run while the machine it runs on supplies the
secrets and the paths.

API keys are never read from the config file. They come from the environment
only, because a config file gets committed by accident and an environment
variable does not. A key written into the file is rejected with a message
saying so rather than quietly used.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

try:                                    # 3.11+
    import tomllib
except ModuleNotFoundError:             # pragma: no cover - 3.10 fallback
    import tomli as tomllib  # type: ignore

CONFIG_NAME = "kobokit.toml"
ENV_PREFIX = "KOBOKIT_"

#: Where each provider's key is expected to live.
KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "ollama": None,                     # local, no key
}

DEFAULT_MODEL = {
    "anthropic": "claude-sonnet-4-20250514",
    "openai": "gpt-4o-mini",
    "ollama": "llama3.1:latest",
}

DEFAULT_ENDPOINT = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "ollama": "http://localhost:11434",
}

ALL_STAGES = ("ingest", "convert", "metadata", "repair", "kepub", "validate",
              "recaps", "report")


class ConfigError(Exception):
    """Raised for a configuration a run cannot sensibly start with."""


@dataclass
class AIConfig:
    provider: str = "ollama"
    model: str = ""
    endpoint: str = ""
    #: seconds; a local model on a long chapter is genuinely slow
    timeout: int = 180
    max_tokens: int = 1024
    temperature: float = 0.2
    #: how many chapters to summarise concurrently
    concurrency: int = 2

    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODEL.get(self.provider, "")

    def resolved_endpoint(self) -> str:
        return self.endpoint or DEFAULT_ENDPOINT.get(self.provider, "")

    def api_key(self) -> str | None:
        name = KEY_ENV.get(self.provider)
        if not name:
            return None
        return os.environ.get(name) or None


@dataclass
class PathsConfig:
    source: Path | None = None
    output: Path | None = None
    work: Path | None = None
    backup: Path | None = None

    def resolve(self) -> PathsConfig:
        out = self.output.expanduser().resolve() if self.output else None
        return PathsConfig(
            source=self.source.expanduser().resolve() if self.source else None,
            output=out,
            work=(self.work.expanduser().resolve() if self.work
                  else (out / ".kobokit-work" if out else None)),
            backup=(self.backup.expanduser().resolve() if self.backup
                    else (out / ".kobokit-originals" if out else None)),
        )


@dataclass
class MetadataConfig:
    #: catalogue lookups first; the model only fills what they could not
    sources: tuple[str, ...] = ("openlibrary", "googlebooks")
    use_ai_fallback: bool = True
    #: never overwrite a field the book already states
    overwrite_existing: bool = False
    #: the user's standing rule: full series name and title, never abbreviated
    filename_template: str = "{series} {index:02d} - {title} - {authors}"
    standalone_template: str = "{title} - {authors}"
    request_delay: float = 1.0


@dataclass
class RecapConfig:
    enabled: bool = True
    #: a chapter shorter than this is front matter, not a chapter
    min_chars: int = 2000
    #: never recap the chapter the reader is inside - only what is behind them
    lag_chapters: int = 1
    cast: bool = True
    language: str = "auto"


@dataclass
class ValidateConfig:
    #: a book that still has errors is reported, not shipped, unless this is off
    require_clean: bool = True
    #: accept a result that is merely better than what it replaced
    accept_improvement: bool = False
    epubcheck_jar: str | None = None
    timeout: int = 300


@dataclass
class ConvertConfig:
    #: auto | always | never. A landscape PDF page is nearly always a
    #: two-page spread; split, it reads as two pages on a six-inch screen.
    #: "auto" asks the PDF and splits only pages wide enough to be spreads,
    #: and the splitter still leaves a page whole where artwork crosses the
    #: gutter.
    split_spreads: str = "auto"
    #: convert finished books to kepub, which is what gives a Kobo page
    #: numbers, reading statistics and a reliable saved position
    kepub: bool = True
    #: recompress images in books above this size, in MB. 0 turns it off.
    slim_above_mb: float = 25.0
    #: look for omnibuses and say so in the report; splitting stays manual
    detect_omnibus: bool = True
    #: fill in a plain typographic cover when a book has none
    make_missing_covers: bool = True
    #: device panel for fixed-layout output, WxH
    panel: str = "1072x1448"
    jpeg_quality: int = 88


@dataclass
class RunConfig:
    stages: tuple[str, ...] = ALL_STAGES
    workers: int = 3
    #: keep going when one book fails, rather than ending the batch
    continue_on_error: bool = True
    dry_run: bool = False
    verbose: bool = False
    #: resume from the run state instead of redoing finished books
    resume: bool = True


@dataclass
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    metadata: MetadataConfig = field(default_factory=MetadataConfig)
    convert: ConvertConfig = field(default_factory=ConvertConfig)
    recaps: RecapConfig = field(default_factory=RecapConfig)
    validate: ValidateConfig = field(default_factory=ValidateConfig)
    run: RunConfig = field(default_factory=RunConfig)
    source_file: Path | None = None

    # ---------------------------------------------------------------- load

    @classmethod
    def load(cls, path: Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
        cfg = cls()
        if path is not None and not Path(path).is_file():
            # An explicitly named file that isn't there is a typo, not a
            # reason to silently fall back to defaults.
            raise ConfigError("no config file at %s" % path)
        found = path or cls.discover()
        if found:
            cfg = cfg._merge_file(found)
            cfg.source_file = found
        cfg = cfg._merge_env()
        if overrides:
            cfg = cfg._merge_overrides(overrides)
        cfg.paths = cfg.paths.resolve()
        return cfg

    @staticmethod
    def discover() -> Path | None:
        """Nearest kobokit.toml walking up, then the XDG config dir."""
        here = Path.cwd().resolve()
        for d in (here, *here.parents):
            p = d / CONFIG_NAME
            if p.is_file():
                return p
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        p = xdg / "kobokit" / CONFIG_NAME
        return p if p.is_file() else None

    def _merge_file(self, path: Path) -> Config:
        try:
            with open(path, "rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError("%s is not valid TOML: %s" % (path, exc)) from exc

        _reject_inline_secrets(raw, path)

        paths = raw.get("paths", {})
        p = PathsConfig(
            source=_as_path(paths.get("source")),
            output=_as_path(paths.get("output")),
            work=_as_path(paths.get("work")),
            backup=_as_path(paths.get("backup")),
        )
        return replace(
            self,
            paths=p,
            ai=_fill(AIConfig, self.ai, raw.get("ai", {})),
            metadata=_fill(MetadataConfig, self.metadata, raw.get("metadata", {})),
            convert=_fill(ConvertConfig, self.convert, raw.get("convert", {})),
            recaps=_fill(RecapConfig, self.recaps, raw.get("recaps", {})),
            validate=_fill(ValidateConfig, self.validate, raw.get("validate", {})),
            run=_fill(RunConfig, self.run, raw.get("run", {})),
        )

    def _merge_env(self) -> Config:
        """KOBOKIT_AI_PROVIDER, KOBOKIT_PATHS_OUTPUT, and so on."""
        sections = {"ai": self.ai, "metadata": self.metadata,
                    "convert": self.convert, "recaps": self.recaps,
                    "validate": self.validate, "run": self.run}
        out: dict[str, Any] = {}
        for name, obj in sections.items():
            updates = {}
            for f in obj.__dataclass_fields__:                # type: ignore[attr-defined]
                env = "%s%s_%s" % (ENV_PREFIX, name.upper(), f.upper())
                if env in os.environ:
                    updates[f] = _coerce(getattr(obj, f), os.environ[env])
            if updates:
                out[name] = replace(obj, **updates)

        paths = self.paths
        pu = {}
        for f in ("source", "output", "work", "backup"):
            env = "%sPATHS_%s" % (ENV_PREFIX, f.upper())
            if env in os.environ:
                pu[f] = Path(os.environ[env])
        if pu:
            paths = replace(paths, **pu)

        # the recap generator's own historical variables still work
        ai = out.get("ai", self.ai)
        if "RECAP_MODEL" in os.environ and not ai.model:
            ai = replace(ai, model=os.environ["RECAP_MODEL"])
        if "RECAP_ENDPOINT" in os.environ and not ai.endpoint:
            ai = replace(ai, endpoint=_strip_ollama_path(os.environ["RECAP_ENDPOINT"]))
        out["ai"] = ai
        return replace(self, paths=paths, **out)

    def _merge_overrides(self, o: dict[str, Any]) -> Config:
        cfg = self
        pu = {k: Path(v) for k, v in o.items()
              if k in ("source", "output", "work", "backup") and v is not None}
        if pu:
            cfg = replace(cfg, paths=replace(cfg.paths, **pu))
        for section in ("ai", "metadata", "convert", "recaps", "validate", "run"):
            updates = {k.split(".", 1)[1]: v for k, v in o.items()
                       if k.startswith(section + ".") and v is not None}
            if updates:
                cfg = replace(cfg, **{section: replace(getattr(cfg, section), **updates)})
        return cfg

    # ------------------------------------------------------------ validate

    def check(self, need_paths: bool = True) -> list[str]:
        """Return the problems that would make this run pointless or unsafe."""
        problems: list[str] = []

        if need_paths:
            if not self.paths.source:
                problems.append("no source directory (--in, paths.source)")
            elif not self.paths.source.is_dir():
                problems.append("source is not a directory: %s" % self.paths.source)
            if not self.paths.output:
                problems.append("no output directory (--out, paths.output)")

        if self.paths.source and self.paths.output:
            try:
                if self.paths.output.resolve() == self.paths.source.resolve():
                    problems.append(
                        "output is the same directory as source; that would "
                        "overwrite the files being read")
            except OSError:
                pass

        if self.ai.provider not in KEY_ENV:
            problems.append("unknown ai.provider %r (expected one of %s)"
                            % (self.ai.provider, ", ".join(sorted(KEY_ENV))))
        elif self._ai_needed():
            key_env = KEY_ENV[self.ai.provider]
            if key_env and not self.ai.api_key():
                problems.append(
                    "ai.provider is %r but %s is not set. Export the key, or "
                    "set ai.provider = \"ollama\" for a local model, or pass "
                    "--no-recaps." % (self.ai.provider, key_env))

        bad = [s for s in self.run.stages if s not in ALL_STAGES]
        if bad:
            problems.append("unknown stage(s): %s" % ", ".join(bad))

        if self.run.workers < 1:
            problems.append("run.workers must be at least 1")

        if "validate" in self.run.stages and not shutil.which("java"):
            problems.append(
                "validate needs a JVM and java is not on PATH. Install a JRE, "
                "or drop 'validate' from run.stages.")
        return problems

    def _ai_needed(self) -> bool:
        if "recaps" in self.run.stages and self.recaps.enabled:
            return True
        return "metadata" in self.run.stages and self.metadata.use_ai_fallback

    def describe(self) -> str:
        key = "set" if self.ai.api_key() else (
            "not needed" if self.ai.provider == "ollama" else "MISSING")
        return "\n".join([
            "source   %s" % (self.paths.source or "-"),
            "output   %s" % (self.paths.output or "-"),
            "work     %s" % (self.paths.work or "-"),
            "stages   %s" % ", ".join(self.run.stages),
            "ai       %s / %s  (%s, key %s)" % (
                self.ai.provider, self.ai.resolved_model(),
                self.ai.resolved_endpoint(), key),
            "config   %s" % (self.source_file or "built-in defaults"),
        ])


# -------------------------------------------------------------- helpers


_SECRET_RE = re.compile(r"(api[_-]?key|secret|token|password)", re.I)


def _reject_inline_secrets(raw: dict, path: Path) -> None:
    """A key in the config file is a key in someone's git history."""
    for section, body in raw.items():
        if not isinstance(body, dict):
            continue
        for k, v in body.items():
            if _SECRET_RE.search(k) and isinstance(v, str) and v.strip():
                raise ConfigError(
                    "%s sets [%s] %s. Keys are read from the environment only "
                    "- remove it from the file and export %s instead."
                    % (path, section, k,
                       KEY_ENV.get(raw.get("ai", {}).get("provider", ""),
                                   "the provider's key variable") or "the key"))


def _as_path(v: Any) -> Path | None:
    return Path(str(v)) if v else None


def _coerce(current: Any, text: str) -> Any:
    if isinstance(current, bool):
        return text.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(text)
    if isinstance(current, float):
        return float(text)
    if isinstance(current, tuple):
        return tuple(x.strip() for x in text.split(",") if x.strip())
    return text


def _fill(cls, current, raw: dict):
    if not raw:
        return current
    updates = {}
    for f in cls.__dataclass_fields__:           # type: ignore[attr-defined]
        if f not in raw:
            continue
        v = raw[f]
        if isinstance(getattr(current, f), tuple) and isinstance(v, list):
            v = tuple(v)
        updates[f] = v
    unknown = set(raw) - set(cls.__dataclass_fields__)          # type: ignore[attr-defined]
    if unknown:
        raise ConfigError("unknown setting(s) in [%s]: %s"
                          % (cls.__name__.replace("Config", "").lower(),
                             ", ".join(sorted(unknown))))
    return replace(current, **updates)


def _strip_ollama_path(url: str) -> str:
    """RECAP_ENDPOINT used to include /api/generate; the client adds it now."""
    return re.sub(r"/api/(generate|chat)/?$", "", url.rstrip("/"))
