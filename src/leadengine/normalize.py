"""Normalisation of the identifiers that deduplication depends on."""

from __future__ import annotations

import re

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")
_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")
_EMAIL = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")

# Role mailboxes reach a shared inbox, not the person the copy was written for.
ROLE_LOCAL_PARTS = frozenset(
    {"info", "hello", "contact", "support", "sales", "admin", "office", "team", "hr", "jobs", "noreply", "no-reply"}
)


def normalize_domain(raw: str | None) -> str | None:
    """'HTTPS://www.Acme.example/about?x=1' -> 'acme.example'. Returns None if not a domain.

    Accepts a bare domain, a URL, or an email address, because company lists arrive
    in all three shapes and each must collapse to the same key.
    """
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if "@" in value and "://" not in value:
        value = value.rsplit("@", 1)[1]
    value = _SCHEME.sub("", value)
    value = re.split(r"[/?#]", value, maxsplit=1)[0]
    value = value.split(":", 1)[0].rstrip(".")
    if value.startswith("www."):
        value = value[4:]
    return value if _DOMAIN.match(value) else None


def normalize_email(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    return value if _EMAIL.match(value) else None


def is_role_email(email: str) -> bool:
    return email.split("@", 1)[0] in ROLE_LOCAL_PARTS


# Checked in order: "VP Marketing" must not fall through to the "marketing" manager rule.
_SENIORITY_RULES = (
    ("c_level", re.compile(r"\b(ceo|cmo|coo|cto|cfo|cro|chief|founder|co-founder|owner)\b")),
    ("vp", re.compile(r"\b(vp|svp|evp|vice president)\b")),
    ("director", re.compile(r"\b(director|head of|head)\b")),
    ("manager", re.compile(r"\b(manager|lead)\b")),
)


def seniority_from_title(title: str | None) -> str | None:
    """Map a free-text job title to the leads.seniority enum. None for no title.

    Hunter, Snov and PhantomBuster return only a title; Apollo returns a seniority,
    which its client maps directly.
    """
    if not title or not title.strip():
        return None
    t = title.lower()
    for level, rule in _SENIORITY_RULES:
        if rule.search(t):
            return level
    return "ic"
