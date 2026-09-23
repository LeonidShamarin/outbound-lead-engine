"""Reply classification checks, the eval set's integrity, and the scoring rubric."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from leadengine.llm import FakeLLM
from leadengine.replies import CATEGORIES, ReplyOut, classify_keywords, classify_llm, validate_reply
from leadengine.scoring import rubric_score

EVAL = Path(__file__).resolve().parents[1] / "eval" / "replies.jsonl"


def _eval() -> list[dict]:
    return [json.loads(line) for line in EVAL.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_eval_set_is_balanced_and_labels_are_valid() -> None:
    rows = _eval()
    assert len(rows) == 40 and len({r["id"] for r in rows}) == 40
    counts = Counter(r["label"] for r in rows)
    assert set(counts) == set(CATEGORIES)
    assert min(counts.values()) >= 6


def test_referral_address_must_appear_in_the_reply() -> None:
    text = "Talk to Olena: olena.bondar@northwind.example"
    ok = ReplyOut(category="referral", referral_email="olena.bondar@northwind.example", summary="s")
    made_up = ReplyOut(category="referral", referral_email="olena@northwind.example", summary="s")
    stray = ReplyOut(category="ooo", referral_email="olena.bondar@northwind.example", summary="s")
    assert validate_reply(ok, text) == []
    assert "does not appear" in validate_reply(made_up, text)[0]
    assert "must be null" in validate_reply(stray, text)[0]


def test_invented_referral_address_is_repaired() -> None:
    text = "Please talk to marta@riverlabs.example instead."
    answers = iter([
        '{"category": "referral", "referral_email": "marta.k@riverlabs.example", "summary": "s"}',
        '{"category": "referral", "referral_email": "marta@riverlabs.example", "summary": "s"}',
    ])
    log = []
    r = classify_llm(FakeLLM(lambda *_: next(answers)), "openai/gpt-oss-20b", text, log)
    assert r.referral_email == "marta@riverlabs.example"
    assert [c.outcome for c in log] == ["invalid", "repaired"]


@pytest.mark.parametrize("text,expected", [
    ("Not interested and stop emailing me.", "unsubscribe"),  # strongest signal wins
    ("Out of office until Monday, contact ops@x.example meanwhile.", "ooo"),
    ("Talk to anna@x.example instead.", "referral"),
    ("Sorry, busy right now.", "neutral"),
])
def test_keyword_baseline_priority(text, expected) -> None:
    assert classify_keywords(text)[0] == expected


def test_keyword_baseline_accuracy_is_recorded_not_assumed() -> None:
    rows = _eval()
    hits = sum(classify_keywords(r["text"])[0] == r["label"] for r in rows)
    # The number itself goes in the README; this only guards against the eval
    # silently becoming trivial for the baseline.
    assert 10 <= hits < 40


@pytest.mark.parametrize("lead,score", [
    ({"seniority": "vp", "industry": "saas", "size_band": "201-500", "country": "DE"}, 5),
    ({"seniority": "director", "industry": "saas", "size_band": "201-500", "country": "DE"}, 4),
    ({"seniority": "vp", "industry": "logistics", "size_band": "201-500", "country": "DE"}, 4),
    ({"seniority": "ic", "industry": "logistics", "size_band": "201-500", "country": "DE"}, 3),
    ({"seniority": "ic", "industry": "logistics", "size_band": "1000+", "country": "DE"}, 2),
    ({"seniority": "ic", "industry": "logistics", "size_band": "1000+", "country": "PL"}, 1),
    ({"seniority": "c_level", "industry": "saas", "size_band": "11-50", "country": "US"}, 1),
])
def test_rubric(lead, score) -> None:
    assert rubric_score(lead)[0] == score
