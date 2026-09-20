"""The provider interface, and the retry behaviour every provider shares.

Deliberately built on urllib rather than each vendor's SDK. This tool runs on
a Kobo owner's laptop, sometimes inside a restricted VM, and the value of
`pip install epubkit` pulling in nothing at all is worth more here than the
convenience of a client library. The request shapes used are the stable,
documented ones.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


class AIError(Exception):
    """A call that cannot be retried into success."""


class AIUnavailable(AIError):
    """The endpoint is not reachable, or the model is not installed there.

    Separate from AIError because it is almost always a setup problem with a
    specific fix, and the caller should say so rather than reporting a book as
    broken.
    """


@dataclass
class Completion:
    text: str
    model: str
    #: None when the provider does not report usage
    input_tokens: int | None = None
    output_tokens: int | None = None


class Provider:
    """Text in, text out. Providers implement `_call`."""

    name = "base"

    def __init__(self, model: str, endpoint: str, api_key: str | None = None,
                 timeout: int = 180, max_tokens: int = 1024,
                 temperature: float = 0.2):
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature

    # -- public ---------------------------------------------------------

    def complete(self, prompt: str, system: str | None = None,
                 retries: int = 3) -> Completion:
        last: Exception | None = None
        for attempt in range(retries):
            try:
                return self._call(prompt, system)
            except AIUnavailable:
                raise                       # retrying a missing model is futile
            except (urllib.error.URLError, TimeoutError, AIError) as exc:
                last = exc
                if attempt == retries - 1:
                    break
                # jittered backoff: several books are summarised at once and
                # synchronised retries just rebuild the same pile-up
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
        raise AIError("%s failed after %d attempts: %s"
                      % (self.name, retries, last))

    def health(self) -> str:
        """One short line describing whether this provider can be used."""
        try:
            r = self.complete("Reply with the single word: ok", retries=1)
        except AIUnavailable as exc:
            return "unavailable: %s" % exc
        except Exception as exc:                        # noqa: BLE001
            return "error: %s" % exc
        return "ok (%s said %r)" % (self.model, r.text.strip()[:40])

    # -- subclass hook ---------------------------------------------------

    def _call(self, prompt: str, system: str | None) -> Completion:
        raise NotImplementedError

    # -- shared plumbing -------------------------------------------------

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              **headers})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            if exc.code in (401, 403):
                raise AIUnavailable(
                    "%s rejected the credentials (HTTP %d). Check the API key "
                    "in the environment. %s" % (self.name, exc.code, detail)
                ) from exc
            if exc.code == 404:
                raise AIUnavailable(
                    "%s has no model %r (HTTP 404). %s"
                    % (self.name, self.model, detail)) from exc
            if exc.code == 429 or exc.code >= 500:
                raise AIError("%s HTTP %d: %s" % (self.name, exc.code, detail)) from exc
            raise AIError("%s HTTP %d: %s" % (self.name, exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise AIUnavailable(
                "cannot reach %s at %s (%s). If this is a local model, check "
                "the server is running and the endpoint is right."
                % (self.name, self.endpoint, exc.reason)) from exc
