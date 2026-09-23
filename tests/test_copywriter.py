"""The copy validator: the design's rules plus the no-invented-facts rule."""

from __future__ import annotations

import pytest

from leadengine.copywriter import CopyOut, LeadContext, validate_copy


def ctx(**variables) -> LeadContext:
    return LeadContext(
        lead_id="l1", first_name="Anna", last_name="Keller", title="VP Marketing", company_name="Bluestack",
        domain="bluestack.example", industry="saas", size_band="201-500", country="DE",
        theory_name="t", hypothesis="h",
        variables=variables or {"funding_round": "Series B, $24M, announced in June 2026",
                                "hiring_pace": "7 open sales and marketing roles posted in the 30 days before 2026-09-01"},
    )


GOOD_A = ("Saw Bluestack closed the Series B in June and has 7 sales and marketing roles open.\n\n"
          "When a team grows that fast, the board asks which channel drives pipeline, and Tallybird answers that.\n\n"
          "Is attribution on your list before the new hires start?")
GOOD_B = ("Question: after the June Series B, who at Bluestack owns the answer to which channel produced pipeline?\n\n"
          "Tallybird gives growth leads that answer without a spreadsheet.\n\n"
          "Worth comparing notes on how you report it today?")


def copy(**kw) -> CopyOut:
    base = {"subject": "your series b and hiring", "body_a": GOOD_A, "body_b": GOOD_B,
            "approach_a": "observation", "approach_b": "question"}
    return CopyOut(**{**base, **kw})


def test_a_good_email_passes() -> None:
    assert validate_copy(copy(), ctx()) == []


@pytest.mark.parametrize("field,text,expected", [
    ("body_a", GOOD_A.replace("Is attribution", "Three of our customers fixed this. Is attribution"),
     "customer claim"),
    ("body_a", GOOD_A.replace("7 sales", "12 sales"), "numbers not in the lead's data: ['12']"),
    ("body_a", "I hope this finds you well.\n\n" + GOOD_A, "forbidden opener"),
    ("body_a", GOOD_A + " Book a call: https://cal.example/x", "hard CTA"),
    ("body_a", GOOD_A.replace("Bluestack closed", "{company_name} closed"), "placeholder"),
    ("body_a", GOOD_A.replace("Is attribution on your list before the new hires start?", "Let me know."),
     "soft question"),
    ("body_b", GOOD_A, "identical"),
    ("body_a", "We leverage synergy. " + GOOD_A, "buzzword"),
])
def test_each_rule_rejects(field, text, expected) -> None:
    problems = validate_copy(copy(**{field: text}), ctx())
    assert any(expected in p for p in problems), problems


def test_subject_rules() -> None:
    assert any("words" in p for p in validate_copy(copy(subject="a b c d e f g"), ctx()))
    assert any("lowercase" in p for p in validate_copy(copy(subject="Your Series B Plans"), ctx()))
    assert any("punctuation" in p for p in validate_copy(copy(subject="your series b!"), ctx()))
    # A proper noun from the data may keep its capital.
    assert validate_copy(copy(subject="bluestack and the June round"), ctx()) == []


def test_too_long_and_too_many_paragraphs() -> None:
    long = " ".join(["word"] * 100) + " Bluestack?"
    assert any("words" in p for p in validate_copy(copy(body_a=long), ctx()))
    four = "Bluestack a.\n\nb.\n\nc.\n\nd?"
    assert any("paragraphs" in p for p in validate_copy(copy(body_a=four), ctx()))


def test_email_must_use_a_specific_fact_from_the_variables() -> None:
    generic = ("Growth teams often struggle with attribution.\n\nTallybird helps with that.\n\n"
               "Is this on your radar?")
    assert any("specific fact" in p for p in validate_copy(copy(body_a=generic), ctx()))
