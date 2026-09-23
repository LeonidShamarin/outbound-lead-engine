"""Clients for each provider. Each turns its API's shape into a list of Contact.

Prices are the per-unit numbers from the original design's cost table
(IMPLEMENTATION.md, section "cost per 1000 leads"). They are planning assumptions
for the mock, not quotes, and they live here only, so a changed price is one edit.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from leadengine.normalize import is_role_email, normalize_email, seniority_from_title
from leadengine.providers.http import Ledger, ProviderError, RetryPolicy, send

PRICES_USD = {
    "apollo_match": 0.016,       # $79 plan / 5000 credits, one credit per revealed email; search is free
    "hunter_search": 0.01,       # per domain-search call
    "snov_search": 0.012,        # per domain-emails call
    "phantombuster_lead": 0.05,  # per lead returned
    "findymail_verify": 0.012,   # per verified address
}
APOLLO_SENIORITY = {"c_suite": "c_level", "vp": "vp", "director": "director", "manager": "manager", "entry": "ic"}
MAX_PAGES = 10  # per provider per company; a list longer than this is not a target account


@dataclass
class Contact:
    source: str
    email: str | None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    seniority: str | None = None
    role: bool = False
    confidence: int | None = None
    cost_usd: float = 0.0  # what this one person cost directly (match, per-lead fee, verification)
    verify_result: str | None = None

    @property
    def name_key(self) -> str | None:
        if not (self.first_name and self.last_name):
            return None
        return f"{self.first_name}|{self.last_name}".lower()


class _Client:
    name = ""

    def __init__(self, http: httpx.Client, base_url: str, api_key: str, *,
                 policy: RetryPolicy = RetryPolicy(), sleep: Callable[[float], None] = time.sleep) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.policy = policy
        self.sleep = sleep

    def _send(self, method: str, path: str, endpoint: str, ledger: Ledger, cost_usd: float = 0.0, **kw):
        return send(self.http, method, self.base_url + path, provider=self.name, endpoint=endpoint,
                    ledger=ledger, policy=self.policy, sleep=self.sleep, cost_usd=cost_usd, **kw)

    def find(self, domain: str, known: set[str], ledger: Ledger) -> list[Contact]:
        raise NotImplementedError


class ApolloClient(_Client):
    """Search is free but masks emails; each reveal is a billed people/match call."""

    name = "apollo"

    def find(self, domain: str, known: set[str], ledger: Ledger) -> list[Contact]:
        headers = {"X-Api-Key": self.api_key}
        people: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            resp, rec = self._send("POST", "/v1/mixed_people/search", "people_search", ledger, headers=headers,
                                   json={"q_organization_domains": domain, "page": page, "per_page": 25})
            data = resp.json()
            batch = data.get("people") or []
            rec.results = len(batch)
            people.extend(batch)
            if not batch or page >= int(data.get("pagination", {}).get("total_pages", 1)):
                break

        out: list[Contact] = []
        for p in people:
            key = f"{p.get('first_name')}|{p.get('last_name')}".lower()
            if p.get("first_name") and p.get("last_name") and key in known:
                continue  # already have this person; do not pay to reveal them again
            resp, rec = self._send("POST", "/v1/people/match", "people_match", ledger,
                                   cost_usd=PRICES_USD["apollo_match"], headers=headers, json={"id": p["id"]})
            person = resp.json().get("person") or {}
            rec.results = 1 if person.get("email") else 0
            out.append(Contact(
                source=self.name, email=person.get("email"), first_name=p.get("first_name"),
                last_name=p.get("last_name"), title=p.get("title"),
                seniority=APOLLO_SENIORITY.get(p.get("seniority") or ""),
                cost_usd=PRICES_USD["apollo_match"],
            ))
        return out


class HunterClient(_Client):
    name = "hunter"

    def find(self, domain: str, known: set[str], ledger: Ledger) -> list[Contact]:
        out: list[Contact] = []
        limit, offset = 25, 0
        for _ in range(MAX_PAGES):
            resp, rec = self._send("GET", "/v2/domain-search", "domain_search", ledger,
                                   cost_usd=PRICES_USD["hunter_search"],
                                   params={"domain": domain, "limit": limit, "offset": offset, "api_key": self.api_key})
            data = resp.json()
            emails = (data.get("data") or {}).get("emails") or []
            rec.results = len(emails)
            for e in emails:
                out.append(Contact(
                    source=self.name, email=e.get("value"), first_name=e.get("first_name"),
                    last_name=e.get("last_name"), title=e.get("position"),
                    seniority=seniority_from_title(e.get("position")),
                    role=e.get("type") == "generic", confidence=e.get("confidence"),
                ))
            offset += len(emails)
            if not emails or offset >= int((data.get("meta") or {}).get("results", 0)):
                break
        return out


class SnovClient(_Client):
    """OAuth token cached for 50 of its 60 minutes; a 401 refreshes it once."""

    name = "snov"
    TOKEN_REUSE_S = 50 * 60

    def __init__(self, *args, client_secret: str = "", clock: Callable[[], float] = time.monotonic, **kw) -> None:
        super().__init__(*args, **kw)
        self.client_secret = client_secret
        self.clock = clock
        self._token: str | None = None
        self._token_at = 0.0

    def _get_token(self, ledger: Ledger) -> str:
        if self._token is None or self.clock() - self._token_at >= self.TOKEN_REUSE_S:
            resp, _ = self._send("POST", "/v1/oauth/access_token", "oauth_token", ledger, data={
                "grant_type": "client_credentials", "client_id": self.api_key, "client_secret": self.client_secret,
            })
            self._token = resp.json()["access_token"]
            self._token_at = self.clock()
        return self._token

    def _page(self, domain: str, last_id: int, ledger: Ledger) -> dict:
        body = {"domain": domain, "type": "all", "limit": 25, "lastId": last_id}
        for refreshed in (False, True):
            headers = {"Authorization": f"Bearer {self._get_token(ledger)}"}
            try:
                resp, rec = self._send("POST", "/v2/domain-emails-with-info", "domain_emails", ledger,
                                       cost_usd=PRICES_USD["snov_search"], headers=headers, json=body)
            except ProviderError as e:
                if e.status == 401 and not refreshed:
                    self._token = None
                    continue
                raise
            data = resp.json()
            rec.results = len(data.get("emails") or [])
            return data
        raise ProviderError(self.name, "token rejected right after refresh", 401)

    def find(self, domain: str, known: set[str], ledger: Ledger) -> list[Contact]:
        out: list[Contact] = []
        last_id = 0
        for _ in range(MAX_PAGES):
            data = self._page(domain, last_id, ledger)
            emails = data.get("emails") or []
            for e in emails:
                out.append(Contact(
                    source=self.name, email=e.get("email"), first_name=e.get("firstName"),
                    last_name=e.get("lastName"), title=e.get("position"),
                    seniority=seniority_from_title(e.get("position")), role=e.get("type") == "generic",
                ))
            new_last = int(data.get("lastId") or 0)
            # Stop on a short page, and also when the cursor did not move: trusting
            # the API to say "done" is how a paging loop runs forever.
            if len(emails) < 25 or new_last <= last_id:
                break
            last_id = new_last
        return out


class PhantomBusterClient(_Client):
    """Launch, poll a bounded number of times, fetch. Billed per lead returned."""

    name = "phantombuster"

    def __init__(self, *args, agent_id: str = "sales-nav-export", max_polls: int = 20,
                 poll_interval_s: float = 30.0, **kw) -> None:
        super().__init__(*args, **kw)
        self.agent_id = agent_id
        self.max_polls = max_polls
        self.poll_interval_s = poll_interval_s
        self.empty_runs = 0  # finished with nothing; a streak of these means the LinkedIn cookie expired

    def find(self, domain: str, known: set[str], ledger: Ledger) -> list[Contact]:
        headers = {"X-Phantombuster-Key": self.api_key}
        resp, _ = self._send("POST", "/api/v2/agents/launch", "launch", ledger, headers=headers,
                             json={"id": self.agent_id, "argument": {"domain": domain}})
        cid = resp.json()["containerId"]
        for _ in range(self.max_polls):
            resp, _ = self._send("GET", "/api/v2/containers/fetch", "fetch_status", ledger,
                                 headers=headers, params={"id": cid})
            if resp.json().get("status") == "finished":
                break
            self.sleep(self.poll_interval_s)
        else:
            raise ProviderError(self.name, f"container {cid} not finished after {self.max_polls} polls")

        resp, rec = self._send("GET", "/api/v2/containers/fetch-result-object", "fetch_result", ledger,
                               headers=headers, params={"id": cid})
        rows = json.loads(resp.json().get("resultObject") or "[]")
        rec.results = len(rows)
        rec.cost_usd = round(len(rows) * PRICES_USD["phantombuster_lead"], 4)
        if not rows:
            self.empty_runs += 1
        return [Contact(
            source=self.name, email=r.get("email"), first_name=r.get("firstName"), last_name=r.get("lastName"),
            title=r.get("title"), seniority=seniority_from_title(r.get("title")),
            cost_usd=PRICES_USD["phantombuster_lead"],
        ) for r in rows]


class FindymailClient(_Client):
    name = "findymail"

    def verify(self, email: str, ledger: Ledger) -> str:
        resp, rec = self._send("POST", "/api/verify", "verify", ledger, cost_usd=PRICES_USD["findymail_verify"],
                               headers={"Authorization": f"Bearer {self.api_key}"}, json={"email": email})
        rec.results = 1
        result = (resp.json().get("verification") or {}).get("result")
        return result if result in ("verified", "risky", "invalid") else "invalid"


def usable_email(c: Contact) -> str | None:
    """The contact's email if it is worth verifying: well-formed and not a shared inbox."""
    email = normalize_email(c.email)
    if email is None or c.role or is_role_email(email):
        return None
    return email
