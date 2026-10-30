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

from . import legacy

try:                                    # 3.11+
    import tomllib
except ModuleNotFoundError:             # pragma: no cover - 3.10 fallback
    import tomli as tomllib  # type: ignore

CONFIG_NAME = "epubkit.toml"
ENV_PREFIX = "EPUBKIT_"

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

ALL_STAGES = ("ingest", "convert", "split", "metadata", "repair", "kepub", "validate",
              "recaps", "report", "shelves")


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
    #: Ollama only: tokens the model reads and writes per request (num_ctx).
    #: Ollama's own default (2048 or 4096) is smaller than a recap prompt, and
    #: it silently cuts the START of a prompt that does not fit - the rules
    #: and the answer format. 8192 is gemma2's maximum; each request slot
    #: costs memory in proportion. 0 leaves it to Ollama.
    context: int = 8192
    #: Ollama only: how long the model stays loaded after a request, so it is
    #: not reloaded between books. "" leaves it to Ollama (5 minutes).
    keep_alive: str = "30m"

    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODEL.get(self.provider, "")

    def resolved_endpoint(self) -> str:
        return self.endpoint or DEFAULT_ENDPOINT.get(self.provider, "")

    def api_key(self) -> str | None:
        name = KEY_ENV.get(self.provider)
        if not name:
            return None
        return os.environ.get(name) or None


def _env(suffix: str) -> str | None:
    """EPUBKIT_<suffix>, falling back to the old KOBOKIT_<suffix>."""
    name = ENV_PREFIX + suffix
    if name in os.environ:
        return os.environ[name]
    return legacy.old_env(suffix)


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
                  else (legacy.dir_or_old(out / ".epubkit-work", legacy.OLD_WORK_DIR)
                        if out else None)),
            backup=(self.backup.expanduser().resolve() if self.backup
                    else (legacy.dir_or_old(out / ".epubkit-originals",
                                            legacy.OLD_BACKUP_DIR)
                          if out else None)),
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
    #: final step: suggest Kids / Teens / Adult for every finished book
    suggest_shelves: bool = True
    #: let that step look the book up in Open Library / Google Books
    shelf_lookup: bool = True
    #: then move each book into a folder named for its library (Kids, Teens,
    #: Adult, Cookbooks), or _Review when the choice needs a look first
    sort_into_libraries: bool = True


@dataclass
class RecapConfig:
    enabled: bool = True
    #: a chapter shorter than this is front matter, not a chapter
    min_chars: int = 2000
    #: unused since recaps went down to the passage: the device now shows only
    #: passages the bookmark is past. Kept so existing config files still load.
    lag_chapters: int = 1
    #: a chapter is recapped in passages of about this many words, so a reader
    #: partway through a chapter still gets everything up to their page
    passage_words: int = 800
    #: the who's who: everyone met or merely heard of, from the paragraph that
    #: first names them
    cast: bool = True
    #: also write the chapter-level recaps.tsv and cast.tsv that copies of
    #: recap.sh from before passages read. Off: the current recap.sh reads the
    #: passage files, and the old ones only repeat the same text
    legacy_files: bool = False
    #: who the relationships are described against. Empty: worked out from
    #: the book. Set it when a run is limited to one book or one series
    main_character: str = ""
    #: keep each passage's answer on disk until the book is written, so a run
    #: that is stopped picks up where it left off instead of starting over
    resume: bool = True
    #: ask the model only for people who are new, met for the first time, or
    #: whose role changed, rather than everyone again in every passage. Less
    #: for the model to write; passages then go in batches of ai.concurrency
    lean_cast: bool = False
    #: auto | en | fr. Auto reads the book: a French book gets French prompts
    #: and French wording throughout, an English one English
    language: str = "auto"
    #: tell each passage who the reader has met so far in this book, and who
    #: they are, so the who's who builds on itself instead of starting over
    #: every few pages. Passages then go in batches of ai.concurrency
    running_memory: bool = True
    #: also give each passage the last few recaps, and the story so far from
    #: the series memory. Off: only people are passed on, and the recap text
    #: itself is written as before
    running_story: bool = False
    #: keep a memory file for each series (who is who to the main character,
    #: the story so far) and give each book what the books before it built.
    #: Needs the book's series and number; see epubkit/series_memory.py
    series_memory: bool = True
    #: where those files live; empty: ~/.local/share/epubkit/series
    series_dir: str = ""
    #: auto | yes | no - is the book told as "I" by its main character?
    #: Auto reads the narration with the dialogue taken out
    first_person: str = "auto"
    #: keep the recap text a book already carries, word for word, and write
    #: only the who's who again. A book without recaps is left alone
    cast_only: bool = False
    #: like cast_only for a book that has recaps; a book without them gets
    #: recaps and a who's who in full
    keep_recaps: bool = False
    #: passages | book. How the who's who is written: from each passage's
    #: answer as it goes (passages), or from the whole book at once - names
    #: found in the text, one entry per person updated only when something
    #: lasting changes, every entry checked against the book (book; see
    #: epubkit/stages/cast.py); or facts: the series' names list decides who
    #: is listed and entries are written from facts gathered chapter by
    #: chapter (epubkit/stages/cast_facts.py; without a names list it is
    #: "book"). With cast_only, "book" and "facts" do not read the passages
    cast_method: str = "passages"
    #: with cast_method = "book", write what it did with each book, and every
    #: entry, to <this folder>/<book>.cast.txt. Empty: no report
    cast_report_dir: str = ""


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
    #: look for omnibuses and say so in the report
    detect_omnibus: bool = True
    #: auto | report | off. "auto" splits a file that holds several books, when
    #: the boundaries come from its own table of contents, before anything else
    #: runs; "report" only says so; "off" does not look.
    split_omnibus: str = "auto"
    #: fill in a plain typographic cover when a book has none
    make_missing_covers: bool = True
    #: device panel for fixed-layout output, WxH
    panel: str = "1072x1448"
    jpeg_quality: int = 88
    #: JPEG quality for picture books (comics, fixed layout): new rebuilds,
    #: and existing ones the slim step recompresses. 0 leaves picture books
    #: alone. 80 looks the same on a Clara Colour at about 60% of the size.
    comic_quality: int = 80


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
        """Nearest epubkit.toml walking up, then the XDG config dir."""
        here = Path.cwd().resolve()
        for d in (here, *here.parents):
            p = d / CONFIG_NAME
            if p.is_file():
                return p
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        p = xdg / "epubkit" / CONFIG_NAME
        if p.is_file():
            return p
        old = legacy.old_config_path()
        if old:
            legacy.notice("using %s; rename it to %s" % (old, CONFIG_NAME))
        return old

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
        """EPUBKIT_AI_PROVIDER, EPUBKIT_PATHS_OUTPUT, and so on."""
        sections = {"ai": self.ai, "metadata": self.metadata,
                    "convert": self.convert, "recaps": self.recaps,
                    "validate": self.validate, "run": self.run}
        out: dict[str, Any] = {}
        for name, obj in sections.items():
            updates = {}
            for f in obj.__dataclass_fields__:                # type: ignore[attr-defined]
                suffix = "%s_%s" % (name.upper(), f.upper())
                val = _env(suffix)
                if val is not None:
                    updates[f] = _coerce(getattr(obj, f), val)
            if updates:
                out[name] = replace(obj, **updates)

        paths = self.paths
        pu = {}
        for f in ("source", "output", "work", "backup"):
            val = _env("PATHS_%s" % f.upper())
            if val is not None:
                pu[f] = Path(val)
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

    def notices(self) -> list[str]:
        """Things worth saying that are not problems."""
        out = []
        src, dst = self.paths.source, self.paths.output
        if src and dst:
            try:
                if src.resolve() in dst.resolve().parents:
                    out.append("the library (%s) sits inside the source folder."
                               " Finished books there are left out of the scan,"
                               " so they are not processed twice." % dst.name)
            except OSError:
                pass
        return out

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
