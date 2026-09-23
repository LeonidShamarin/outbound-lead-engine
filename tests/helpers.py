"""Hand-built worlds for tests, where who-is-in-which-provider is chosen, not random."""

from __future__ import annotations

import httpx

from leadengine.enrich import Clients, mock_clients
from leadengine.providers.http import RetryPolicy
from leadengine.providers.mock import Faults, MockProviders


def person(domain: str, first: str | None, last: str | None, *, email: str | None = None,
           status: str = "valid", role: bool = False, title: str = "VP Sales", seniority: str = "vp",
           apollo: bool = False, hunter: bool = False, snov: bool = False, phantombuster: bool = False) -> dict:
    return {
        "company_domain": domain, "first_name": first, "last_name": last, "title": title,
        "seniority": seniority, "email": email or f"{(first or 'x').lower()}.{(last or 'y').lower()}@{domain}",
        "email_status": status, "role_email": role,
        "providers": {"apollo": apollo, "hunter": hunter, "snov": snov, "phantombuster": phantombuster},
    }


def world(*people: dict) -> dict:
    domains = sorted({p["company_domain"] for p in people})
    return {
        "seed": 0,
        "providers": ["apollo", "hunter", "snov", "phantombuster"],
        "companies": [{"domain": d, "name": d.split(".")[0], "industry": "saas", "size_band": "11-50",
                       "country": "DE", "timezone": "Europe/Berlin", "raw_inputs": [d]} for d in domains],
        "people": list(people),
        "intake": domains,
    }


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def setup(w: dict, faults: Faults | None = None, **policy) -> tuple[MockProviders, Clients, list[float], Clock]:
    """Mock + clients with a fake clock and a sleep that only records the wait."""
    clock = Clock()
    mock = MockProviders(w, faults, clock=clock)
    slept: list[float] = []
    clients = mock_clients(mock, sleep=slept.append, clock=clock, policy=RetryPolicy(**policy))
    return mock, clients, slept, clock


def http_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))
