"""The guided setup behind `kobokit init`.

Every answer is checked while the person is still standing there: a folder is
counted, an endpoint is called, a model list is fetched from the server rather
than typed from memory. A wizard that writes down whatever it is told and lets
the run discover the problems is only an editor with extra steps.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .config import CONFIG_NAME, Config
from .pipeline import BOOK_SUFFIXES

PROVIDERS = ("ollama", "anthropic", "openai")
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
DEFAULT_ENDPOINT = {
    "ollama": "http://localhost:11434",
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
}
DEFAULT_MODEL = {
    "ollama": "llama3.1:latest",
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4o-mini",
}


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ asking


def ask(prompt: str, default: str = "", allow_empty: bool = False) -> str:
    suffix = " [%s]: " % default if default else ": "
    while True:
        try:
            answer = input(prompt + suffix).strip()
        except (EOFError, KeyboardInterrupt):
            raise Cancelled from None
        answer = answer or default
        if answer or allow_empty:
            return answer
        print("  Needs an answer.")


def ask_yes(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = input("%s %s " % (prompt, hint)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise Cancelled from None
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def ask_choice(prompt: str, options, default: str) -> str:
    options = list(options)
    print(prompt)
    for i, opt in enumerate(options, 1):
        print("  %d) %s%s" % (i, opt, "  (default)" if opt == default else ""))
    while True:
        answer = ask("Choose 1-%d or type a name" % len(options), default)
        if answer in options:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        print("  Not one of those.")


# ----------------------------------------------------------------- probing


def count_books(folder: Path) -> int:
    n = 0
    for p in folder.rglob("*"):
        if p.is_file() and not p.name.startswith(".") \
                and p.name.lower().endswith(BOOK_SUFFIXES):
            n += 1
    return n


def ollama_models(endpoint: str, timeout: int = 6) -> list[str] | None:
    """The models the server actually has, or None if it cannot be reached."""
    try:
        with urllib.request.urlopen(endpoint.rstrip("/") + "/api/tags",
                                    timeout=timeout) as fh:
            data = json.loads(fh.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    return sorted(m.get("name", "") for m in data.get("models", []) if m.get("name"))


# ------------------------------------------------------------------- steps


def ask_source(current: str = "") -> Path:
    while True:
        raw = ask("Where are the books you want processed?", current)
        folder = Path(raw).expanduser()
        if folder.is_dir():
            # absolute, so the config keeps working from any directory
            folder = folder.resolve()
        if not folder.is_dir():
            print("  There is no folder at %s." % folder)
            if ask_yes("  Use it anyway?", default=False):
                return folder
            continue
        n = count_books(folder)
        if n:
            print("  %d book(s) there." % n)
            return folder
        print("  That folder has no books in it that kobokit recognises.")
        if ask_yes("  Use it anyway?", default=False):
            return folder


def ask_output(source: Path, current: str = "") -> Path:
    while True:
        raw = ask("Where should the finished library go?", current)
        folder = Path(raw).expanduser().absolute()
        if folder == source.resolve() or (folder.exists()
                                          and folder.resolve() == source.resolve()):
            print("  That is the source folder. The library needs to be "
                  "somewhere else so the originals stay untouched.")
            continue
        if not folder.exists():
            print("  %s does not exist yet; it will be created." % folder)
        return folder


def ask_ai(current_provider="ollama", current_model="", current_endpoint=""):
    provider = ask_choice("\nWhich model should write recaps and fill gaps in "
                          "metadata?", PROVIDERS, current_provider)
    endpoint = ask("Endpoint", current_endpoint or DEFAULT_ENDPOINT[provider])
    model = current_model or DEFAULT_MODEL[provider]

    if provider == "ollama":
        print("  Asking %s what it has..." % endpoint)
        models = ollama_models(endpoint)
        if models is None:
            print("  Could not reach it. Check the server is running and the "
                  "address is right - a model on another machine needs that "
                  "machine's address here, not localhost.")
            if not ask_yes("  Carry on anyway?", default=True):
                raise Cancelled
        elif not models:
            print("  It answered, but has no models pulled yet.")
        else:
            model = ask_choice("  It has:", models,
                               model if model in models else models[0])
    else:
        model = ask("Model", model)
        env = KEY_ENV[provider]
        if not os.environ.get(env):
            print("  Note: %s is not set in this shell. kobokit never stores a "
                  "key in the config file, so export it before a run:" % env)
            print("    export %s=..." % env)
    return provider, model, endpoint


# ------------------------------------------------------------------ output


def render(source: Path, output: Path, provider: str, model: str,
           endpoint: str, recaps: bool, concurrency: int) -> str:
    from .cli import EXAMPLE_CONFIG
    body = EXAMPLE_CONFIG
    body = body.replace('source = "~/Downloads/books"', 'source = "%s"' % source)
    body = body.replace('output = "~/Books/kobo"', 'output = "%s"' % output)
    body = body.replace('provider = "ollama"', 'provider = "%s"' % provider)
    body = body.replace('model    = "llama3.1:latest"', 'model    = "%s"' % model)
    body = body.replace('endpoint = "http://localhost:11434"',
                        'endpoint = "%s"' % endpoint)
    body = body.replace("concurrency = 2", "concurrency = %d" % concurrency)
    if not recaps:
        body = body.replace("[recaps]\nenabled = true",
                            "[recaps]\nenabled = false")
    return body


def run(target: Path | None = None) -> int:
    """Ask, check, write. Returns a process exit code."""
    existing = None
    target = target or (Path.cwd() / CONFIG_NAME)
    if target.exists():
        print("%s already exists." % target)
        if not ask_yes("Go through setup again and replace it?", default=False):
            print("Left as it is. `kobokit config set` changes one setting.")
            return 0
        try:
            existing = Config.load(target)
        except Exception:                                   # noqa: BLE001
            existing = None

    print("\nkobokit setup - press Enter to take the default in brackets.\n")
    src_default = str(existing.paths.source) if existing and existing.paths.source else ""
    out_default = str(existing.paths.output) if existing and existing.paths.output else ""

    source = ask_source(src_default)
    output = ask_output(source, out_default)
    provider, model, endpoint = ask_ai(
        existing.ai.provider if existing else "ollama",
        existing.ai.model if existing else "",
        existing.ai.endpoint if existing else "")

    print()
    recaps = ask_yes("Write per-chapter recaps into each book? (slow, and it "
                     "is what the model is for)", default=True)
    concurrency = 1
    if recaps and provider != "ollama":
        concurrency = 2

    body = render(source, output, provider, model, endpoint, recaps, concurrency)
    print("\nAbout to write %s:\n" % target)
    print("  source    %s" % source)
    print("  output    %s" % output)
    print("  model     %s / %s  (%s)" % (provider, model, endpoint))
    print("  recaps    %s" % ("on" if recaps else "off"))
    if not ask_yes("\nWrite it?", default=True):
        print("Nothing written.")
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    print("\nWrote %s" % target)
    print("Next: kobokit doctor   (checks the pieces are installed)")
    print("      kobokit check    (confirms this config works)")
    print("      kobokit run -n   (says what it would do, touching nothing)")
    return 0
