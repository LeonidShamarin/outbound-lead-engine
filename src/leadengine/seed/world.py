"""Synthetic world: fictional companies, people, and what each data provider knows.

Nothing here is a real company or person. Every domain ends in `.example`, a TLD
reserved by RFC 2606, so no generated address can ever reach a mailbox.

The world is the ground truth the mock providers answer from (stage 2) and the
simulator sends events for (stage 4). It is deliberately dirty in the ways real
lead lists are: the same company typed three ways, rows that are not domains at
all, role mailboxes, people without a surname, emails that are risky or invalid,
and contacts that only the most expensive provider has.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field

PROVIDERS = ("apollo", "hunter", "snov", "phantombuster")  # cheapest first

COUNTRIES = {  # country -> IANA timezone the send-window logic will need
    "DE": "Europe/Berlin", "PL": "Europe/Warsaw", "UA": "Europe/Kyiv", "NL": "Europe/Amsterdam",
    "GB": "Europe/London", "ES": "Europe/Madrid", "US": "America/New_York", "CA": "America/Toronto",
}
INDUSTRIES = ("saas", "ecommerce", "logistics", "fintech", "edtech", "martech", "manufacturing")
SIZE_BANDS = ("11-50", "51-200", "201-500", "501-1000", "1000+")
WORDS_A = ("north", "blue", "quiet", "bright", "iron", "cedar", "lumen", "vector", "harbor", "atlas",
           "nova", "peak", "river", "solid", "clear", "rapid", "orbit", "maple", "silver", "prime")
WORDS_B = ("wind", "field", "stack", "labs", "works", "cart", "flow", "grid", "path", "forge",
           "logix", "pay", "learn", "metrics", "supply", "base", "loop", "point", "yard", "line")
FIRST_NAMES = ("Anna", "Marek", "Olena", "Jonas", "Sofia", "Lukas", "Iryna", "Tom", "Eva", "Daniel",
               "Marta", "Oskar", "Nadia", "Pieter", "Clara", "Mateo", "Kateryna", "Ben", "Lena", "Hugo")
LAST_NAMES = ("Novak", "Keller", "Bondar", "Visser", "Moreau", "Kowalski", "Hart", "Brandt", "Lopez",
              "Shevchenko", "Meyer", "Jansen", "Wojcik", "Fischer", "Reyes", "Tkachenko", "Berg", "Quinn")
TITLES = {
    "c_level": ("CEO", "CMO", "COO", "Chief Revenue Officer"),
    "vp": ("VP Marketing", "VP Sales", "VP Growth"),
    "director": ("Head of Marketing", "Director of Partnerships", "Head of Growth"),
    "manager": ("Marketing Manager", "Event Manager", "Partnership Manager"),
    "ic": ("Content Specialist", "SDR", "Marketing Analyst"),
}


@dataclass
class Company:
    domain: str
    name: str
    industry: str
    size_band: str
    country: str
    timezone: str
    raw_inputs: list[str] = field(default_factory=list)


@dataclass
class Person:
    company_domain: str
    first_name: str | None
    last_name: str | None
    title: str
    seniority: str
    email: str
    email_status: str  # valid | risky | invalid
    role_email: bool
    providers: dict[str, bool] = field(default_factory=dict)


def _raw_variants(rng: random.Random, domain: str) -> list[str]:
    """How the same company shows up in a hand-made list."""
    forms = [
        domain,
        f"https://www.{domain}/",
        f"http://{domain.upper()}",
        f"www.{domain}",
        f"https://{domain}/about?ref=list",
        f"sales@{domain}",
    ]
    return [forms[0]] + rng.sample(forms[1:], k=rng.choice((0, 0, 0, 1, 2)))


def _email(rng: random.Random, first: str | None, last: str | None, domain: str) -> str:
    f, l = (first or "").lower(), (last or "").lower()
    if f and l:
        pattern = rng.choice((f"{f}.{l}", f"{f[0]}{l}", f"{f}", f"{f}_{l}"))
    else:
        pattern = f or l or "contact"
    return f"{pattern}@{domain}"


def build_world(seed: int = 42, n_companies: int = 250) -> dict:
    if not 1 <= n_companies <= 5000:
        raise ValueError("n_companies must be between 1 and 5000")
    rng = random.Random(seed)

    companies: list[Company] = []
    used: set[str] = set()
    attempts = 0
    # Bounded: the name space is 20*20*7 = 2800 combinations, so a request that
    # cannot be satisfied stops instead of looping forever.
    while len(companies) < n_companies:
        attempts += 1
        if attempts > n_companies * 50:
            raise ValueError(f"could not generate {n_companies} unique companies")
        a, b = rng.choice(WORDS_A), rng.choice(WORDS_B)
        industry = rng.choice(INDUSTRIES)
        domain = f"{a}{b}-{industry}.example" if rng.random() < 0.5 else f"{a}{b}.example"
        if domain in used:
            continue
        used.add(domain)
        country = rng.choice(tuple(COUNTRIES))
        companies.append(Company(
            domain=domain, name=f"{a.title()}{b.title()}", industry=industry,
            size_band=rng.choice(SIZE_BANDS), country=country, timezone=COUNTRIES[country],
            raw_inputs=_raw_variants(rng, domain),
        ))

    people: list[Person] = []
    for c in companies:
        seen_emails: set[str] = set()
        for _ in range(rng.randint(1, 6)):
            seniority = rng.choices(tuple(TITLES), weights=(1, 2, 3, 3, 2))[0]
            first = rng.choice(FIRST_NAMES)
            last = None if rng.random() < 0.08 else rng.choice(LAST_NAMES)
            role = rng.random() < 0.07
            email = f"{rng.choice(('info', 'hello', 'sales'))}@{c.domain}" if role else _email(rng, first, last, c.domain)
            if email in seen_emails:
                continue
            seen_emails.add(email)
            # Cheaper providers cover fewer people, some people exist only in the
            # most expensive source, and a few are in none. That spread is what the
            # cascade in stage 2 has to handle; tests measure it rather than assume it.
            providers = {
                "apollo": rng.random() < 0.55,
                "hunter": rng.random() < 0.45,
                "snov": rng.random() < 0.35,
                "phantombuster": True,
            }
            if not any(providers[p] for p in PROVIDERS[:3]) and rng.random() < 0.5:
                providers["phantombuster"] = rng.random() < 0.8
            people.append(Person(
                company_domain=c.domain, first_name=first, last_name=last,
                title=rng.choice(TITLES[seniority]), seniority=seniority, email=email,
                email_status=rng.choices(("valid", "risky", "invalid"), weights=(82, 12, 6))[0],
                role_email=role, providers=providers,
            ))

    # Rows in the intake list that are not companies at all.
    junk = ["", "   ", "n/a", "see sheet 2", "localhost", "http://", "acme"]
    intake = [raw for c in companies for raw in c.raw_inputs] + rng.sample(junk, k=4)
    rng.shuffle(intake)

    return {
        "seed": seed,
        "providers": list(PROVIDERS),
        "companies": [asdict(c) for c in companies],
        "people": [asdict(p) for p in people],
        "intake": intake,
    }
