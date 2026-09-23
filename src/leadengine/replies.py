"""Reply classification: what a prospect's answer means for the pipeline.

Six classes from the design. Two changes:
* no self-reported confidence: on an earlier project the model reported 1.00 on 36
  of 40 answers, wrong ones included, so the number carries no signal;
* the referral address is checked by code: it must literally appear in the reply.
  A model that "extracts" an address it made up would route the lead to nobody.

A keyword baseline sits next to the LLM, so the eval shows what the model adds.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from leadengine.llm import LLMCall, LLMClient, structured

Category = Literal["positive", "negative", "ooo", "unsubscribe", "referral", "neutral"]
CATEGORIES: tuple[str, ...] = ("positive", "negative", "ooo", "unsubscribe", "referral", "neutral")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


class ReplyOut(BaseModel):
    category: Category
    referral_email: str | None
    summary: str = Field(max_length=200)


REPLY_SYSTEM = """You classify replies to cold outreach emails into exactly one category:

- positive: genuine interest; asks a question, asks for a call, a meeting or more information
- negative: an explicit no ("not interested", "no thanks", "we already have a tool for this")
- ooo: out-of-office or vacation auto-reply, parental leave, "back on <date>"
- unsubscribe: an explicit request to stop emailing ("remove me", "unsubscribe", "do not contact")
- referral: points to another person ("talk to X instead", "I forwarded this to our head of Y")
- neutral: none of the above; vague, "maybe later", "busy now", an auto-reply that is not out-of-office

Subtle cases:
- "Sorry, busy right now" alone is neutral, not negative
- "Maybe in 6 months" is neutral
- "Stop emailing me" is unsubscribe even if it also says no: the strongest signal wins
- A polite no with a reason is negative
- An out-of-office that names a colleague to contact meanwhile is ooo, not referral

referral_email: only for referral, and only an address written in the reply; otherwise null.
summary: one sentence."""


def validate_reply(r: ReplyOut, text: str) -> list[str]:
    problems = []
    if r.category == "referral":
        if r.referral_email and r.referral_email.lower() not in text.lower():
            problems.append(f"referral_email {r.referral_email!r} does not appear in the reply; "
                            "use an address from the reply or null")
    elif r.referral_email:
        problems.append("referral_email must be null unless the category is referral")
    return problems


def classify_llm(llm: LLMClient, model: str, text: str, log: list[LLMCall]) -> ReplyOut:
    return structured(llm, ReplyOut, step="reply", model=model, system=REPLY_SYSTEM,
                      user=f"Reply:\n{text}\n\nClassify.", log=log,
                      validate=lambda r: validate_reply(r, text))


_RULES: list[tuple[str, re.Pattern]] = [
    ("unsubscribe", re.compile(r"unsubscribe|remove me|stop (emailing|contacting|sending)|do not contact|"
                               r"take me off", re.I)),
    ("ooo", re.compile(r"out of (the )?office|on (vacation|holiday|leave)|parental leave|limited access to email|"
                       r"back on|return(ing)? on", re.I)),
    ("referral", re.compile(r"talk to|reach out to|contact .* instead|forwarded (this|your)|cc'?d|"
                            r"right person|better person", re.I)),
    ("negative", re.compile(r"not interested|no thanks|no, thank|not a fit|already (use|have)|pass on this|"
                            r"not for us", re.I)),
    ("positive", re.compile(r"interested|send (me )?(more|details|info)|let'?s (talk|chat)|happy to|"
                            r"set up a call|tell me more|how does|what does it cost|pricing", re.I)),
]


def classify_keywords(text: str) -> tuple[str, str | None]:
    """Baseline: first matching rule in priority order, else neutral."""
    for cat, rx in _RULES:
        if rx.search(text):
            email = None
            if cat == "referral":
                m = _EMAIL.search(text)
                email = m.group(0) if m else None
            return cat, email
    return "neutral", None
