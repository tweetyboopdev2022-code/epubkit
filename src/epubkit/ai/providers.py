"""The three backends, and the factory that picks one from config."""
from __future__ import annotations

import re

from .base import AIError, AIUnavailable, Completion, Provider

#: Reasoning models (qwen3, deepseek-r1 and friends) narrate their thinking
#: inside these before answering. Left in place it corrupts every reply this
#: tool parses: a stray brace in the reasoning breaks the metadata JSON, and
#: the word SUMMARY appearing mid-thought derails a recap.
THINK_RE = re.compile(r"<(think|thinking|reasoning)\b[^>]*>.*?</\1>",
                      re.S | re.I)
#: an unclosed block, which happens when the model is cut off mid-thought
OPEN_THINK_RE = re.compile(r"<(think|thinking|reasoning)\b[^>]*>.*\Z", re.S | re.I)


def strip_thinking(text: str) -> str:
    if not text or "<" not in text:
        return text
    text = THINK_RE.sub("", text)
    text = OPEN_THINK_RE.sub("", text)
    return text.strip()


class AnthropicProvider(Provider):
    name = "anthropic"
    API_VERSION = "2023-06-01"

    def _call(self, prompt, system):
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        data = self._post(
            self.endpoint + "/v1/messages", payload,
            {"x-api-key": self.api_key or "",
             "anthropic-version": self.API_VERSION},
        )
        parts = [b.get("text", "") for b in data.get("content", [])
                 if b.get("type") == "text"]
        usage = data.get("usage") or {}
        return Completion("".join(parts), data.get("model", self.model),
                          usage.get("input_tokens"), usage.get("output_tokens"))


class OpenAIProvider(Provider):
    """Also covers anything speaking the OpenAI chat-completions shape."""

    name = "openai"

    def _call(self, prompt, system):
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        headers = {}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        base = self.endpoint
        if not base.endswith("/v1"):
            base += "/v1"
        data = self._post(base + "/chat/completions", payload, headers)
        choices = data.get("choices") or []
        if not choices:
            raise AIError("openai returned no choices: %s" % str(data)[:200])
        text = (choices[0].get("message") or {}).get("content", "")
        usage = data.get("usage") or {}
        return Completion(text, data.get("model", self.model),
                          usage.get("prompt_tokens"),
                          usage.get("completion_tokens"))


class OllamaProvider(Provider):
    name = "ollama"

    #: Ask a reasoning model not to think out loud. Nothing here benefits from
    #: it: these are short, mechanical summarising jobs, and the thinking costs
    #: minutes a chapter on a local box. Older Ollama builds and models with no
    #: thinking mode reject the field, so the first rejection turns it off for
    #: the rest of the run rather than failing the book.
    send_think_flag = True

    def _call(self, prompt, system):
        options = {"temperature": self.temperature, "num_predict": self.max_tokens}
        if self.context:
            options["num_ctx"] = self.context
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": True,
            "options": options,
        }
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive
        if system:
            payload["system"] = system
        if self.send_think_flag:
            payload["think"] = False

        data = self._post(self.endpoint + "/api/generate", payload, {})
        if "error" in data:
            msg = str(data["error"])
            if self.send_think_flag and "think" in msg.lower():
                self.send_think_flag = False
                payload.pop("think", None)
                data = self._post(self.endpoint + "/api/generate", payload, {})
                msg = str(data.get("error", ""))
        if data.get("error"):
            msg = str(data["error"])
            # Ollama answers 200 with an error body when the model is absent
            if "not found" in msg.lower() or "try pulling" in msg.lower():
                raise AIUnavailable(
                    "ollama has no model %r. Run: ollama pull %s"
                    % (self.model, self.model))
            raise AIError("ollama: %s" % msg)

        # A model that thinks anyway - because the flag was refused, or because
        # it simply ignores it - still has to come out the other side clean.
        text = strip_thinking(data.get("response", ""))
        if not text and data.get("thinking"):
            # newer Ollama returns the reasoning in its own field; if that is
            # all there is, the model answered with nothing but thought
            raise AIError("%s replied with reasoning and no answer; try "
                          "a larger model or a non-reasoning one" % self.model)
        return Completion(text,
                          data.get("model", self.model),
                          data.get("prompt_eval_count"),
                          data.get("eval_count"))


PROVIDERS = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
}


def build(ai_config) -> Provider:
    """Construct the provider named by an AIConfig."""
    try:
        cls = PROVIDERS[ai_config.provider]
    except KeyError:
        raise AIError("unknown provider %r; expected one of %s"
                      % (ai_config.provider, ", ".join(sorted(PROVIDERS)))) from None
    return cls(
        model=ai_config.resolved_model(),
        endpoint=ai_config.resolved_endpoint(),
        api_key=ai_config.api_key(),
        timeout=ai_config.timeout,
        max_tokens=ai_config.max_tokens,
        temperature=ai_config.temperature,
        context=getattr(ai_config, "context", 0),
        keep_alive=getattr(ai_config, "keep_alive", ""),
    )
