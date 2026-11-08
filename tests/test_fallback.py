"""Multi-provider fallback: chain order, fall-through on failure, request shape.

No network: the OpenAI-compatible client runs against httpx.MockTransport, and the
chain logic runs against tiny stub clients.
"""

from __future__ import annotations

import json

import httpx
import pytest
from helpers import http_for

from leadengine.llm import (
    Completion,
    FallbackClient,
    GroqClient,
    LLMError,
    OpenAICompatClient,
    build_llm,
)


class _Stub:
    """Minimal LLMClient: either raises LLMError or returns a fixed Completion."""

    def __init__(self, name: str, *, fail: bool = False, text: str = '{"ok": true}') -> None:
        self.name, self.fail, self.text, self.calls = name, fail, text, 0

    def complete(self, **_: object) -> Completion:
        self.calls += 1
        if self.fail:
            raise LLMError(f"{self.name} down")
        return Completion(self.text, 10, 5)


def _args() -> dict:
    return dict(model="m", system="s", user="u", schema_name="S", schema={"type": "object"})


def test_first_working_provider_short_circuits() -> None:
    a, b = _Stub("a"), _Stub("b")
    out = FallbackClient([("a", a), ("b", b)]).complete(**_args())
    assert out.text == '{"ok": true}'
    assert (a.calls, b.calls) == (1, 0)


def test_falls_through_to_next_on_llmerror() -> None:
    a, b, c = _Stub("a", fail=True), _Stub("b", fail=True), _Stub("c", text='{"v": 1}')
    out = FallbackClient([("a", a), ("b", b), ("c", c)]).complete(**_args())
    assert out.text == '{"v": 1}'
    assert (a.calls, b.calls, c.calls) == (1, 1, 1)


def test_all_providers_failing_raises_naming_each() -> None:
    fb = FallbackClient([("groq:GROQ_API_KEY", _Stub("x", fail=True)),
                         ("gemini:GEMINI_API_KEY", _Stub("y", fail=True))])
    with pytest.raises(LLMError) as e:
        fb.complete(**_args())
    assert "groq:GROQ_API_KEY" in str(e.value) and "gemini:GEMINI_API_KEY" in str(e.value)


def test_build_llm_orders_primary_then_aikeys_then_klyuchi2_and_dedups() -> None:
    env = {
        "GROQ_API_KEY": "gsk_primary", "GEMINI_API_KEY": "AIza_a",
        "OPENAI_API_KEY": "sk-proj-a", "XAI_API_KEY": "xai-a",
        "GEMINI_API_KEY_2": "AQ.one", "GEMINI_API_KEY_3": "AQ.two", "GROQ_API_KEY_2": "gsk_second",
    }
    names = [n for n, _ in build_llm("groq", env=env).members]
    # Groq primary listed once (its ai-keys.env slot is the same key -> deduped),
    # then gemini/openai/xai, then the ключі2.txt keys last.
    assert names == ["groq:GROQ_API_KEY", "gemini:GEMINI_API_KEY", "openai:OPENAI_API_KEY",
                     "xai:XAI_API_KEY", "gemini:GEMINI_API_KEY_2", "gemini:GEMINI_API_KEY_3",
                     "groq:GROQ_API_KEY_2"]


def test_gemini_primary_dedups_against_its_aikeys_slot() -> None:
    names = [n for n, _ in build_llm("gemini", env={"GEMINI_API_KEY": "AIza_x", "GROQ_API_KEY": "gsk_y"}).members]
    assert names == ["gemini:GEMINI_API_KEY", "groq:GROQ_API_KEY"]


def test_build_llm_single_provider_returns_bare_client() -> None:
    assert isinstance(build_llm("groq", env={"GROQ_API_KEY": "gsk_x"}), GroqClient)


def test_build_llm_no_keys_raises() -> None:
    with pytest.raises(LLMError):
        build_llm("groq", env={})


def test_openai_compat_client_shape() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"v": 2}'}}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 3}})

    c = OpenAICompatClient("gemini", "AQ.key", http=http_for(handler))
    out = c.complete(model="ignored-logical-id", system="sys", user="u", schema_name="S",
                     schema={"type": "object", "properties": {"v": {"type": "integer"}}})
    assert out.text == '{"v": 2}' and out.prompt_tokens == 7
    assert seen["url"].endswith("/v1beta/openai/chat/completions")
    assert seen["auth"] == "Bearer AQ.key"
    assert seen["body"]["model"] == "gemini-2.5-flash"  # provider default, not the logical id
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert "schema" in seen["body"]["messages"][0]["content"].lower()  # schema pasted into system


def test_openai_compat_client_maps_4xx_to_llmerror() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    c = OpenAICompatClient("openai", "sk-bad", http=http_for(handler))
    with pytest.raises(LLMError):
        c.complete(model="m", system="s", user="u", schema_name="S", schema={"type": "object"})
