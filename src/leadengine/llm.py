"""LLM calls with a schema, a validator, one self-repair, and a cost record.

Two different retries, never mixed:
* transport (429, 5xx, network) is `providers.http.send`, without the model;
* content (the answer does not parse, fails Pydantic, or fails a business check)
  is a repair: the model sees its own answer and the reasons it was rejected.

Groq's strict structured output guarantees the JSON shape, not the content: a
score of 7, a 140-word email or an invented number all fit the schema. So every
answer still goes through `model_validate` and the step's own validator.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from leadengine.providers.http import Ledger, ProviderError, RetryPolicy, send

# USD per 1M tokens (input, output), console.groq.com/docs/models, checked 2026-09-23.
PRICES_PER_M = {
    "openai/gpt-oss-20b": (0.075, 0.30),
    "openai/gpt-oss-120b": (0.15, 0.60),
}
GROQ_URL = "https://api.groq.com/openai/v1"

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The request itself failed (transport, auth, provider down)."""


class LLMInvalid(Exception):
    """The model answered, but after the repair the answer was still rejected."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems)[:500])
        self.problems = problems


@dataclass
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int


@dataclass
class LLMCall:
    step: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    outcome: str = "ok"  # ok | repaired | invalid | error
    error: str | None = None
    lead_id: str | None = None


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = PRICES_PER_M.get(model, (0.0, 0.0))
    return round((prompt_tokens * pin + completion_tokens * pout) / 1_000_000, 6)


class LLMClient(Protocol):
    def complete(self, *, model: str, system: str, user: str, schema_name: str, schema: dict,
                 temperature: float) -> Completion: ...


_DROP = {"title", "default", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
         "minLength", "maxLength", "pattern", "minItems", "maxItems", "format"}


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema reshaped for strict mode: refs inlined, every field required,
    no extra keys, and no numeric/length keywords (strict mode rejects them; Pydantic
    enforces them afterwards anyway)."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node, depth: int = 0):
        if depth > 20:
            raise ValueError("schema nested deeper than 20 levels")
        if isinstance(node, list):
            return [walk(n, depth + 1) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[1]]), depth + 1)
        out = {k: walk(v, depth + 1) for k, v in node.items() if k not in _DROP}
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    return walk(raw)


class GroqClient:
    """OpenAI-compatible chat completions with strict JSON schema output."""

    def __init__(self, api_key: str, *, http: httpx.Client | None = None, base_url: str = GROQ_URL,
                 policy: RetryPolicy = RetryPolicy(), sleep: Callable[[float], None] = time.sleep,
                 reasoning_effort: str = "low", max_completion_tokens: int = 2000) -> None:
        self.api_key = api_key
        self.http = http or httpx.Client(timeout=60.0)
        self.base_url = base_url.rstrip("/")
        self.policy = policy
        self.sleep = sleep
        self.reasoning_effort = reasoning_effort
        self.max_completion_tokens = max_completion_tokens

    def complete(self, *, model: str, system: str, user: str, schema_name: str, schema: dict,
                 temperature: float = 0.0) -> Completion:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "max_completion_tokens": self.max_completion_tokens,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
        }
        if model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = self.reasoning_effort
        try:
            resp, _ = send(self.http, "POST", f"{self.base_url}/chat/completions", provider="groq",
                           endpoint="chat", ledger=Ledger(), policy=self.policy, sleep=self.sleep,
                           headers={"Authorization": f"Bearer {self.api_key}"}, json=body)
        except ProviderError as e:
            raise LLMError(str(e)) from e
        data = resp.json()
        usage = data.get("usage") or {}
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected response shape: {str(data)[:200]}") from e
        return Completion(text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))


# --- multi-provider fallback -------------------------------------------------
#
# Chain order (see ~/.claude memory ai-provider-fallback-chain-pet-projects):
#   current provider -> ai-keys.env [gemini, groq, openai, xai] -> ключі2.txt last.
# Groq, OpenAI and xAI are natively OpenAI-compatible; Gemini exposes an
# OpenAI-compatible endpoint. Each fallback provider ignores the caller's logical
# model id (a Groq id like "openai/gpt-oss-120b" is meaningless to OpenAI) and
# uses its own default below.
PROVIDER_ENDPOINTS: dict[str, tuple[str, str]] = {
    # provider -> (base_url, default model)
    "groq": (GROQ_URL, "openai/gpt-oss-120b"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "xai": ("https://api.x.ai/v1", "grok-2-latest"),
}

_PRIMARY_ENV = {"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY",
                "openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY"}
# The four keys from D:\secrets\ai-keys.env, in file order.
_AIKEYS_ENV = [("gemini", "GEMINI_API_KEY"), ("groq", "GROQ_API_KEY"),
               ("openai", "OPENAI_API_KEY"), ("xai", "XAI_API_KEY")]
# The ключі2.txt keys — tried last.
_KLYUCHI2_ENV = [("gemini", "GEMINI_API_KEY_2"), ("gemini", "GEMINI_API_KEY_3"),
                 ("groq", "GROQ_API_KEY_2")]


class OpenAICompatClient:
    """OpenAI-compatible chat client for any provider in the fallback chain.

    Unlike GroqClient it asks for `response_format: json_object` and pastes the
    schema into the system prompt, because strict `json_schema` is not supported
    uniformly across Gemini/OpenAI/xAI. `structured()` still validates with
    Pydantic and repairs, so a merely well-formed JSON object is enough here.
    """

    def __init__(self, provider: str, api_key: str, *, http: httpx.Client | None = None,
                 base_url: str | None = None, model: str | None = None,
                 policy: RetryPolicy = RetryPolicy(), sleep: Callable[[float], None] = time.sleep,
                 max_completion_tokens: int = 2000) -> None:
        base_default, model_default = PROVIDER_ENDPOINTS.get(provider, ("", ""))
        self.provider = provider
        self.api_key = api_key
        self.http = http or httpx.Client(timeout=60.0)
        self.base_url = (base_url or base_default).rstrip("/")
        self.model = model or model_default
        self.policy = policy
        self.sleep = sleep
        self.max_completion_tokens = max_completion_tokens

    def complete(self, *, model: str, system: str, user: str, schema_name: str, schema: dict,
                 temperature: float = 0.0) -> Completion:
        system_with_schema = (
            f"{system}\n\n# Output: one JSON object matching this schema "
            f"(no prose, no markdown fence):\n{json.dumps(schema, ensure_ascii=False)}"
        )
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_with_schema},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": self.max_completion_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            resp, _ = send(self.http, "POST", f"{self.base_url}/chat/completions", provider=self.provider,
                           endpoint="chat", ledger=Ledger(), policy=self.policy, sleep=self.sleep,
                           headers={"Authorization": f"Bearer {self.api_key}"}, json=body)
        except ProviderError as e:
            raise LLMError(str(e)) from e
        data = resp.json()
        usage = data.get("usage") or {}
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected response shape: {str(data)[:200]}") from e
        return Completion(text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))


class FallbackClient:
    """Try each provider in order; move to the next only on a request failure
    (`LLMError`: transport, auth, provider down). A content problem is not a
    provider's fault — it is raised by `structured()` for repair and never
    switches provider, so a bad answer does not quietly burn the whole chain."""

    def __init__(self, members: list[tuple[str, LLMClient]]) -> None:
        if not members:
            raise LLMError("fallback chain is empty: no provider keys configured")
        self.members = members

    def complete(self, *, model: str, system: str, user: str, schema_name: str, schema: dict,
                 temperature: float = 0.0) -> Completion:
        errors: list[str] = []
        for name, client in self.members:
            try:
                return client.complete(model=model, system=system, user=user, schema_name=schema_name,
                                       schema=schema, temperature=temperature)
            except LLMError as e:
                errors.append(f"{name}: {str(e)[:150]}")
        raise LLMError("all providers failed -> " + " | ".join(errors))


def _make_client(provider: str, key: str, http: httpx.Client | None = None) -> LLMClient:
    # The primary Groq path keeps its strict-schema client; every other provider
    # uses the portable json_object client.
    if provider == "groq":
        return GroqClient(key, http=http)
    return OpenAICompatClient(provider, key, http=http)


def build_llm(primary: str = "groq", *, env: dict | None = None,
              http: httpx.Client | None = None) -> LLMClient:
    """Assemble the provider fallback chain from the environment.

    Order: the project's current provider first, then the four ai-keys.env keys
    (gemini, groq, openai, xai), then the ключі2.txt keys last. Entries whose key
    env var is unset are skipped; duplicate (provider, key) pairs collapse, so a
    Groq-primary project does not list its own key twice. A single configured
    provider returns a bare client, with no wrapper overhead. Raises LLMError when
    nothing is configured."""
    env = os.environ if env is None else env
    ordered: list[tuple[str, str]] = [(primary, _PRIMARY_ENV[primary])]
    ordered += _AIKEYS_ENV + _KLYUCHI2_ENV

    members: list[tuple[str, LLMClient]] = []
    seen: set[tuple[str, str]] = set()
    for provider, var in ordered:
        key = (env.get(var) or "").strip()
        if not key or (provider, key) in seen:
            continue
        seen.add((provider, key))
        members.append((f"{provider}:{var}", _make_client(provider, key, http=http)))
    if not members:
        raise LLMError("no provider keys configured (set GROQ_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY or XAI_API_KEY)")
    if len(members) == 1:
        return members[0][1]
    return FallbackClient(members)


def _pydantic_problems(e: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in err['loc']) or 'answer'}: {err['msg']}" for err in e.errors()[:10]]


def structured(
    llm: LLMClient,
    out: type[T],
    *,
    step: str,
    model: str,
    system: str,
    user: str,
    log: list[LLMCall],
    validate: Callable[[T], list[str]] | None = None,
    max_repairs: int = 1,
    temperature: float = 0.0,
    lead_id: str | None = None,
) -> T:
    """Ask, validate, repair at most `max_repairs` times. Every request goes to `log`."""
    schema = strict_schema(out)
    prompt = user
    problems: list[str] = []
    for attempt in range(max_repairs + 1):
        t0 = time.monotonic()
        call = LLMCall(step=step, model=model, lead_id=lead_id)
        log.append(call)
        try:
            c = llm.complete(model=model, system=system, user=prompt, schema_name=out.__name__,
                             schema=schema, temperature=temperature)
        except LLMError as e:
            call.outcome, call.error = "error", str(e)[:500]
            call.latency_ms = int((time.monotonic() - t0) * 1000)
            raise
        call.latency_ms = int((time.monotonic() - t0) * 1000)
        call.prompt_tokens, call.completion_tokens = c.prompt_tokens, c.completion_tokens
        call.cost_usd = cost_usd(model, c.prompt_tokens, c.completion_tokens)
        try:
            obj = out.model_validate_json(c.text)
            problems = validate(obj) if validate else []
        except ValidationError as e:
            problems = _pydantic_problems(e)
        if not problems:
            call.outcome = "repaired" if attempt else "ok"
            return obj
        call.outcome, call.error = "invalid", "; ".join(problems)[:500]
        prompt = (f"{user}\n\nYour previous answer was:\n{c.text[:3000]}\n\n"
                  f"It was rejected for these reasons:\n- " + "\n- ".join(problems) +
                  "\n\nWrite a corrected answer that fixes every reason.")
    raise LLMInvalid(problems)


def groq_key_from_file(path: str) -> str:
    """The secrets file holds `GROQ_API_KEY=gsk_...`; take the token, not the line."""
    with open(path, encoding="utf-8") as f:
        m = re.search(r"gsk_[A-Za-z0-9]+", f.read())
    if not m:
        raise RuntimeError(f"no gsk_ token in {path}")
    return m.group(0)


@dataclass
class FakeLLM:
    """Deterministic stand-in for tests: answers come from a function of the request."""

    answer: Callable[[str, str, str], str]  # (schema_name, system, user) -> JSON text
    prompt_tokens: int = 500
    completion_tokens: int = 150
    requests: list[dict] = field(default_factory=list)

    def complete(self, *, model: str, system: str, user: str, schema_name: str, schema: dict,
                 temperature: float = 0.0) -> Completion:
        self.requests.append({"model": model, "schema_name": schema_name, "user": user, "schema": schema})
        return Completion(self.answer(schema_name, system, user), self.prompt_tokens, self.completion_tokens)
