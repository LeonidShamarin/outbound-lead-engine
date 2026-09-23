"""Cold email generation: two variants per lead, checked in code before they are stored.

The validator carries over the design's rules (length, forbidden phrases,
placeholders, A differs from B) and adds the one the design left to the prompt:
**no invented facts**. The model may only state numbers and names that appear in
the lead's own data. A 90-word email that says "three of our customers" is exactly
what the design's own example did, and it is the claim a recipient can check.
Facts are checked by code, not by asking the model whether it made them up.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

# Fictional sender. Nothing here is a real company.
BRAND = {
    "company": "Tallybird",
    "sender": "Mira",
    "what_it_does": "pipeline attribution for B2B growth teams: which channel produced which pipeline",
}

MAX_WORDS = 95  # the design's 90 plus its own 5-word slack
MAX_SUBJECT_WORDS = 6
FORBIDDEN = [
    (re.compile(r"\{|\}|\[[A-Za-z][^\]]*\]"), "unfilled placeholder"),
    (re.compile(r"\bi hope (this|you|the|all)\b", re.I), "forbidden opener 'I hope ...'"),
    (re.compile(r"hope you('re| are) (doing )?well", re.I), "forbidden opener 'hope you are well'"),
    (re.compile(r"\b(i )?(came across|noticed) your profile\b", re.I), "generic opener about a profile"),
    (re.compile(r"\bi wanted to reach out\b", re.I), "forbidden phrase 'I wanted to reach out'"),
    (re.compile(r"\b(synergy|synergies|leverage|circle back|touch base)\b", re.I), "buzzword"),
    (re.compile(r"companies like yours", re.I), "generic phrase 'companies like yours'"),
    (re.compile(r"\bbook (a |some )?(time|call|meeting|demo)\b|calendly|https?://", re.I),
     "hard CTA or link (use a soft question)"),
    (re.compile(r"\bour (customers|clients)\b", re.I), "customer claim that cannot be checked"),
]
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_CAPWORD = re.compile(r"\b[A-Z][a-zA-Z]+\b")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


class CopyOut(BaseModel):
    subject: str = Field(max_length=80)
    body_a: str = Field(max_length=900)
    body_b: str = Field(max_length=900)
    approach_a: str = Field(max_length=200)
    approach_b: str = Field(max_length=200)


@dataclass
class LeadContext:
    lead_id: str
    first_name: str | None
    last_name: str | None
    title: str | None
    company_name: str | None
    domain: str
    industry: str | None
    size_band: str | None
    country: str | None
    theory_name: str
    hypothesis: str
    variables: dict[str, str]

    def fact_text(self) -> str:
        """Everything the email may state as fact."""
        parts = [self.first_name, self.last_name, self.title, self.company_name, self.domain, self.industry,
                 self.size_band, self.country, *self.variables.values(), BRAND["company"], BRAND["sender"]]
        return " ".join(p for p in parts if p)


def words(text: str) -> int:
    return len(text.split())


def _anchors(ctx: LeadContext) -> set[str]:
    """Specific tokens from the variables; an email must use at least one."""
    out: set[str] = set()
    for v in ctx.variables.values():
        out |= set(_NUMBER.findall(v))
        out |= {w for w in _CAPWORD.findall(v) if w not in {"Series", "Added", "Opened"}}
    return out


def validate_copy(c: CopyOut, ctx: LeadContext) -> list[str]:
    problems: list[str] = []
    facts = ctx.fact_text()
    fact_numbers = set(_NUMBER.findall(facts))
    proper = {w.lower() for w in _CAPWORD.findall(facts)}

    s = c.subject.strip()
    if not s:
        problems.append("subject is empty")
    if words(s) > MAX_SUBJECT_WORDS:
        problems.append(f"subject has {words(s)} words, max {MAX_SUBJECT_WORDS}")
    if re.search(r"[^\w\s?$'&-]", s) or _EMOJI.search(s):
        problems.append("subject may contain only words, spaces and '?', no other punctuation or emoji")
    caps = [w for w in _CAPWORD.findall(s) if w.lower() not in proper]
    if caps:
        problems.append(f"subject must be lowercase except proper nouns from the data; found {caps}")

    anchors = _anchors(ctx)
    for name, body in (("body_a", c.body_a), ("body_b", c.body_b)):
        b = body.strip()
        n = words(b)
        if n > MAX_WORDS:
            problems.append(f"{name} has {n} words, max 90")
        paragraphs = [p for p in re.split(r"\n\s*\n", b) if p.strip()]
        if len(paragraphs) > 3:
            problems.append(f"{name} has {len(paragraphs)} paragraphs, max 3")
        if not paragraphs or "?" not in paragraphs[-1]:
            problems.append(f"{name} must end with a soft question in its last paragraph")
        if anchors and not any(re.search(rf"\b{re.escape(a)}\b", b) for a in anchors):
            problems.append(f"{name} does not use any specific fact from the variables")
    for name, text in (("subject", c.subject), ("body_a", c.body_a), ("body_b", c.body_b)):
        for rx, why in FORBIDDEN:
            if rx.search(text):
                problems.append(f"{name}: {why}")
        invented = sorted(set(_NUMBER.findall(text)) - fact_numbers)
        if invented:
            problems.append(f"{name}: numbers not in the lead's data: {invented}; state no numbers except those given")
    if " ".join(c.body_a.split()).lower() == " ".join(c.body_b.split()).lower():
        problems.append("body_a and body_b are identical; no real A/B test")
    return problems


COPY_SYSTEM = f"""You write concise B2B cold outreach emails for {BRAND['company']}, which does {BRAND['what_it_does']}.
The sender is {BRAND['sender']}. Sign-off is not needed; it is added by the sending tool.

HARD RULES (a rule broken means the email is rejected by an automatic checker):

SUBJECT:
- 6 words maximum, lowercase except proper nouns, no emoji, no punctuation except a question mark
- It must reference something specific from the Variables, not a generic phrase
- Good: "your series b and the hiring push", "after the segment rollout"
- Bad: "quick question", "introduction", "exploring partnership"

BODY (two variants, body_a and body_b):
- 90 words maximum each, at most 3 short paragraphs separated by a blank line
- First sentence: a specific observation built on one or two Variables
- Middle: one sentence connecting that observation to what {BRAND['company']} does
- Last paragraph: one soft question as the call to action. No calendar links, no URLs, no "book a call"
- Variant A leads with the observation; variant B leads with a question or a pattern. They must differ.

FACTS:
- State only facts that are in Recipient or Variables. Do not invent customers, results, quotes, percentages,
  or any number. The only digits allowed are digits that appear in the Variables or Recipient data.
- Never write "our customers" or "our clients".

FORBIDDEN PHRASES: "I hope this finds you well", "hope you're doing well", "I came across your profile",
"I wanted to reach out", "synergy", "leverage", "circle back", "touch base", "companies like yours",
any placeholder in braces or brackets.

TONE: direct, concrete, peer to peer. Confident, not pushy. Show 60 seconds of homework.

approach_a and approach_b: one line each describing the angle of that variant."""


class TemplateWriter:
    """A deterministic, non-LLM writer for bulk simulation runs.

    It fills a fixed template from the same prompt the LLM gets, so it goes through
    the same validator and the same code path. Emails it writes are plain on
    purpose; LLM email quality is measured separately (eval/results/copy-*.json).
    """

    def complete(self, *, model: str, system: str, user: str, schema_name: str, schema: dict,
                 temperature: float = 0.0):
        from leadengine.llm import Completion

        company = re.search(r"Company: (.+?) \(", user).group(1)
        variables = json.loads(user.split("Variables (use the most specific one or two):\n", 1)[1]
                               .rsplit("\n\nWrite the email.", 1)[0])
        fact = next(iter(variables.values()), "")
        return Completion(json.dumps({
            "subject": "a question about pipeline",
            "body_a": f"Saw that {company} {fact}.\n\n{BRAND['company']} shows which channel produced which "
                      "pipeline.\n\nIs that on your radar this quarter?",
            "body_b": f"Question: after {company} {fact}, who owns channel attribution?\n\n"
                      f"{BRAND['company']} answers that without a spreadsheet.\n\nWorth comparing notes?",
            "approach_a": "observation", "approach_b": "question",
        }), 0, 0)


def copy_user_prompt(ctx: LeadContext) -> str:
    return (
        f"Theory:\n  Name: {ctx.theory_name}\n  Hypothesis: {ctx.hypothesis}\n\n"
        f"Recipient:\n  Name: {ctx.first_name or ''} {ctx.last_name or ''}\n  Title: {ctx.title}\n"
        f"  Company: {ctx.company_name} ({ctx.domain})\n  Industry: {ctx.industry}\n"
        f"  Size: {ctx.size_band} employees\n  Country: {ctx.country}\n\n"
        f"Variables (use the most specific one or two):\n{json.dumps(ctx.variables, indent=1)}\n\n"
        "Write the email."
    )
