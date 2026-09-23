"""LLM plumbing without a network: schema shaping, repair loop, cost, Groq request shape."""

from __future__ import annotations

import json

import httpx
import pytest
from helpers import http_for

from leadengine.llm import (
    FakeLLM,
    GroqClient,
    LLMError,
    LLMInvalid,
    cost_usd,
    groq_key_from_file,
    strict_schema,
    structured,
)
from leadengine.providers.http import RetryPolicy
from leadengine.replies import ReplyOut
from leadengine.scoring import ScoreOut


def test_strict_schema_requires_every_field_and_forbids_extras() -> None:
    s = strict_schema(ReplyOut)
    assert s["additionalProperties"] is False
    assert sorted(s["required"]) == ["category", "referral_email", "summary"]
    assert "maxLength" not in json.dumps(s) and "title" not in s
    assert s["properties"]["category"]["enum"][0] == "positive"


def test_valid_answer_is_accepted_first_time_and_costed() -> None:
    llm = FakeLLM(lambda *_: '{"score": 4, "rationale": "fits", "negative_signals": []}',
                  prompt_tokens=1000, completion_tokens=200)
    log = []
    out = structured(llm, ScoreOut, step="score", model="openai/gpt-oss-20b", system="s", user="u", log=log)
    assert out.score == 4
    assert [c.outcome for c in log] == ["ok"]
    assert log[0].cost_usd == pytest.approx((1000 * 0.075 + 200 * 0.30) / 1e6)


def test_out_of_range_answer_is_repaired_with_the_reason_shown_to_the_model() -> None:
    answers = iter(['{"score": 7, "rationale": "x", "negative_signals": []}',
                    '{"score": 5, "rationale": "x", "negative_signals": []}'])
    llm = FakeLLM(lambda *_: next(answers))
    log = []
    out = structured(llm, ScoreOut, step="score", model="m", system="s", user="u", log=log)
    assert out.score == 5
    assert [c.outcome for c in log] == ["invalid", "repaired"]
    assert "score" in llm.requests[1]["user"] and "rejected" in llm.requests[1]["user"]


def test_business_validator_failure_counts_as_invalid_and_repair_is_bounded() -> None:
    llm = FakeLLM(lambda *_: '{"score": 3, "rationale": "x", "negative_signals": []}')
    log = []
    with pytest.raises(LLMInvalid, match="always wrong"):
        structured(llm, ScoreOut, step="score", model="m", system="s", user="u", log=log,
                   validate=lambda _: ["always wrong"], max_repairs=2)
    assert len(llm.requests) == 3 and [c.outcome for c in log] == ["invalid"] * 3


def test_non_json_answer_is_repaired() -> None:
    answers = iter(["Sure! The score is 4.", '{"score": 4, "rationale": "x", "negative_signals": []}'])
    log = []
    structured(FakeLLM(lambda *_: next(answers)), ScoreOut, step="score", model="m", system="s", user="u", log=log)
    assert [c.outcome for c in log] == ["invalid", "repaired"]


def test_groq_request_uses_strict_schema_and_low_reasoning() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"score": 5, "rationale": "r", "negative_signals": []}'}}],
            "usage": {"prompt_tokens": 321, "completion_tokens": 45}})

    groq = GroqClient("gsk_test", http=http_for(handler), base_url="http://groq")
    c = groq.complete(model="openai/gpt-oss-20b", system="sys", user="usr", schema_name="ScoreOut",
                      schema=strict_schema(ScoreOut), temperature=0.0)
    rf = seen["body"]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert seen["body"]["reasoning_effort"] == "low"
    assert seen["auth"] == "Bearer gsk_test"
    assert (c.prompt_tokens, c.completion_tokens) == (321, 45)


def test_groq_rate_limit_is_retried_and_a_bad_request_is_not() -> None:
    statuses = iter([429, 200])
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        status = next(statuses)
        if status == 429:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {}})

    slept = []
    groq = GroqClient("k", http=http_for(handler), base_url="http://g", sleep=slept.append)
    groq.complete(model="m", system="s", user="u", schema_name="X", schema={}, temperature=0)
    assert len(calls) == 2 and slept == [2.0]

    bad = GroqClient("k", http=http_for(lambda r: httpx.Response(400, json={"error": "schema"})),
                     base_url="http://g", policy=RetryPolicy(max_attempts=3), sleep=lambda _: None)
    with pytest.raises(LLMError, match="400"):
        bad.complete(model="m", system="s", user="u", schema_name="X", schema={}, temperature=0)


def test_transport_error_is_logged_as_error_and_raised() -> None:
    class Down:
        def complete(self, **_):
            raise LLMError("groq: chat answered 503 after 4 attempts")

    log = []
    with pytest.raises(LLMError):
        structured(Down(), ScoreOut, step="score", model="m", system="s", user="u", log=log)
    assert log[0].outcome == "error" and "503" in log[0].error


def test_unknown_model_costs_zero_rather_than_guessing() -> None:
    assert cost_usd("some/new-model", 1000, 1000) == 0.0


def test_key_is_taken_from_a_labelled_line(tmp_path) -> None:
    f = tmp_path / "k.txt"
    f.write_text("GROQ_API_KEY=gsk_abc123XYZ\n", encoding="utf-8")
    assert groq_key_from_file(str(f)) == "gsk_abc123XYZ"
