"""In-process stand-ins for Apollo, Hunter, Snov, PhantomBuster and Findymail.

They answer from the synthetic world with the response shapes and the awkward
parts of the real APIs, because those are what the clients have to get right:

* Apollo search masks emails; revealing one is a separate, billed `people/match`.
* Hunter pages with limit/offset and returns role mailboxes with no name.
* Snov wants an OAuth token that expires after an hour, and pages by `lastId`.
* PhantomBuster is async: launch, poll until finished, then fetch the result. When
  its LinkedIn cookie expires it still finishes with exit code 0, just empty.
* Findymail says verified / risky / invalid.

Routing is by the first path segment (`/apollo/...`, `/hunter/...`), so the same
handler can later be served over real HTTP under `/api/mock/`.

Faults are injected per request: scripted statuses for tests, random rates for
measurement runs. Each random draw is seeded by the request itself (path, query,
body, and how many times that exact request was seen), not by a shared sequence.
Companies are claimed in UUID order, which differs between runs; with a shared
sequence the same world gave 576 verified leads in one run and 568 in the next.
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx

APOLLO_SENIORITY = {"c_level": "c_suite", "vp": "vp", "director": "director", "manager": "manager", "ic": "entry"}
FINDYMAIL_RESULT = {"valid": "verified", "risky": "risky", "invalid": "invalid"}
SNOV_TOKEN_TTL_S = 3600


@dataclass
class Faults:
    rate_429: float = 0.0
    rate_5xx: float = 0.0
    retry_after_s: int = 2
    phantom_silent_empty: float = 0.0
    phantom_polls: tuple[int, int] = (1, 3)  # how many "running" answers before "finished"
    # "<provider>/<path suffix>" -> statuses returned for the next matching calls,
    # before the call is served normally. E.g. {"hunter/v2/domain-search": [429, 503]}.
    script: dict[str, list[int]] = field(default_factory=dict)


def _json(status: int, body: object, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def _person_id(p: dict) -> str:
    return hashlib.sha1(f"{p['company_domain']}|{p['email']}".encode()).hexdigest()[:24]


class MockProviders:
    def __init__(
        self,
        world: dict,
        faults: Faults | None = None,
        seed: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.faults = faults or Faults()
        self._seed = seed
        self._seen: dict[str, int] = {}
        self._clock = clock
        self._lock = threading.Lock()
        self.calls: deque[str] = deque(maxlen=200_000)
        self._by_domain: dict[str, list[dict]] = {}
        self._by_id: dict[str, dict] = {}
        for p in world["people"]:
            self._by_domain.setdefault(p["company_domain"], []).append(p)
            self._by_id[_person_id(p)] = p
        self._snov_tokens: dict[str, float] = {}
        self._containers: dict[str, dict] = {}
        self._script = {k: list(v) for k, v in self.faults.script.items()}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def count(self, prefix: str) -> int:
        with self._lock:
            return sum(1 for c in self.calls if c.startswith(prefix))

    # --- dispatch -----------------------------------------------------------

    def _rng_for(self, material: str) -> random.Random:
        """A generator seeded by this request and its repeat count. Call under the lock."""
        n = self._seen.get(material, 0)
        self._seen[material] = n + 1
        digest = hashlib.sha1(f"{self._seed}|{material}|{n}".encode()).hexdigest()
        return random.Random(int(digest[:16], 16))

    def handle(self, request: httpx.Request) -> httpx.Response:
        provider, _, rest = request.url.path.lstrip("/").partition("/")
        key = f"{provider}/{rest}"
        material = f"{key}?{request.url.query.decode()}|{request.content.decode(errors='replace')}"
        with self._lock:
            self.calls.append(key)
            rng = self._rng_for(material)
        # Handlers read the generator from the request, never from self: with two
        # runs in parallel a shared attribute would be swapped under them.
        request.extensions["mock_rng"] = rng
        fault = self._fault(key, rng)
        if fault is not None:
            return fault
        route = {
            "apollo/v1/mixed_people/search": self._apollo_search,
            "apollo/v1/people/match": self._apollo_match,
            "hunter/v2/domain-search": self._hunter_search,
            "snov/v1/oauth/access_token": self._snov_token,
            "snov/v2/domain-emails-with-info": self._snov_emails,
            "phantombuster/api/v2/agents/launch": self._pb_launch,
            "phantombuster/api/v2/containers/fetch": self._pb_fetch,
            "phantombuster/api/v2/containers/fetch-result-object": self._pb_result,
            "findymail/api/verify": self._verify,
        }.get(key)
        if route is None:
            return _json(404, {"error": f"no route {key}"})
        return route(request)

    def _fault(self, key: str, rng: random.Random) -> httpx.Response | None:
        with self._lock:
            for prefix, queue in self._script.items():
                if key.startswith(prefix) and queue:
                    return self._fault_response(queue.pop(0))
        if key.startswith("snov/v1/oauth"):
            return None  # token endpoint stays up; failures are tested on data calls
        r = rng.random()
        if r < self.faults.rate_429:
            return self._fault_response(429)
        if r < self.faults.rate_429 + self.faults.rate_5xx:
            return self._fault_response(503)
        return None

    def _fault_response(self, status: int) -> httpx.Response:
        headers = {"Retry-After": str(self.faults.retry_after_s)} if status == 429 else None
        return _json(status, {"error": "injected"}, headers)

    def _people(self, domain: str, provider: str, *, persons_only: bool = False) -> list[dict]:
        return [
            p for p in self._by_domain.get(domain, [])
            if p["providers"].get(provider) and not (persons_only and p["role_email"])
        ]

    # --- Apollo -------------------------------------------------------------

    def _apollo_search(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("X-Api-Key"):
            return _json(401, {"error": "missing api key"})
        body = json.loads(request.content or b"{}")
        page = max(int(body.get("page", 1)), 1)
        per_page = min(max(int(body.get("per_page", 25)), 1), 100)
        people = self._people(body.get("q_organization_domains", ""), "apollo", persons_only=True)
        chunk = people[(page - 1) * per_page: page * per_page]
        total_pages = max((len(people) + per_page - 1) // per_page, 1)
        return _json(200, {
            "people": [{
                "id": _person_id(p),
                "first_name": p["first_name"],
                "last_name": p["last_name"],
                "title": p["title"],
                "seniority": APOLLO_SENIORITY[p["seniority"]],
                "email": "email_not_unlocked@domain.com",
                "organization": {"primary_domain": p["company_domain"]},
            } for p in chunk],
            "pagination": {"page": page, "per_page": per_page, "total_entries": len(people),
                           "total_pages": total_pages},
        })

    def _apollo_match(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("X-Api-Key"):
            return _json(401, {"error": "missing api key"})
        p = self._by_id.get(json.loads(request.content or b"{}").get("id", ""))
        if p is None or not p["providers"].get("apollo"):
            return _json(200, {"person": None})
        return _json(200, {"person": {
            "id": _person_id(p), "first_name": p["first_name"], "last_name": p["last_name"],
            "title": p["title"], "seniority": APOLLO_SENIORITY[p["seniority"]], "email": p["email"],
            "email_status": "verified" if p["email_status"] == "valid" else "guessed",
        }})

    # --- Hunter -------------------------------------------------------------

    def _hunter_search(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params
        if not q.get("api_key"):
            return _json(401, {"errors": [{"details": "missing api key"}]})
        limit = min(max(int(q.get("limit", 10)), 1), 100)
        offset = max(int(q.get("offset", 0)), 0)
        people = self._people(q.get("domain", ""), "hunter")
        return _json(200, {
            "data": {"domain": q.get("domain"), "emails": [{
                "value": p["email"],
                "type": "generic" if p["role_email"] else "personal",
                "confidence": self._confidence(p),
                "first_name": None if p["role_email"] else p["first_name"],
                "last_name": None if p["role_email"] else p["last_name"],
                "position": None if p["role_email"] else p["title"],
            } for p in people[offset: offset + limit]]},
            "meta": {"results": len(people), "limit": limit, "offset": offset},
        })

    @staticmethod
    def _confidence(p: dict) -> int:
        # Deterministic per address; lower for addresses that will not verify, as
        # Hunter's score is, though not perfectly: some risky ones score above 80.
        h = int(hashlib.sha1(p["email"].encode()).hexdigest()[:4], 16)
        low, high = {"valid": (86, 99), "risky": (65, 88), "invalid": (35, 70)}[p["email_status"]]
        return low + h % (high - low + 1)

    # --- Snov ---------------------------------------------------------------

    def _snov_token(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        if form.get("grant_type") != ["client_credentials"] or not form.get("client_id"):
            return _json(400, {"error": "invalid_request"})
        with self._lock:
            token = f"snov-{len(self._snov_tokens) + 1}"
            self._snov_tokens[token] = self._clock()
        return _json(200, {"access_token": token, "token_type": "Bearer", "expires_in": SNOV_TOKEN_TTL_S})

    def _snov_emails(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        with self._lock:
            issued = self._snov_tokens.get(token)
        if issued is None or self._clock() - issued >= SNOV_TOKEN_TTL_S:
            return _json(401, {"error": "invalid_token"})
        body = json.loads(request.content or b"{}")
        limit = min(max(int(body.get("limit", 25)), 1), 100)
        last_id = max(int(body.get("lastId", 0)), 0)
        people = self._people(body.get("domain", ""), "snov")
        # Ids are 1-based positions; the cursor is the last id already returned.
        chunk = list(enumerate(people, start=1))[last_id: last_id + limit]
        return _json(200, {
            "success": True, "domain": body.get("domain"), "result": len(people),
            "lastId": chunk[-1][0] if chunk else last_id,
            "emails": [{
                "email": p["email"],
                "firstName": None if p["role_email"] else p["first_name"],
                "lastName": None if p["role_email"] else p["last_name"],
                "position": None if p["role_email"] else p["title"],
                "type": "generic" if p["role_email"] else "personal",
            } for _, p in chunk],
        })

    # --- PhantomBuster ------------------------------------------------------

    def _pb_launch(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("X-Phantombuster-Key"):
            return _json(401, {"error": "missing key"})
        domain = json.loads(request.content or b"{}").get("argument", {}).get("domain", "")
        rng: random.Random = request.extensions["mock_rng"]
        with self._lock:
            # Id from the domain and its launch count, not a global counter, so the
            # fault draws for the polls that follow do not depend on run order.
            n = self._seen.get(f"pb-launch|{domain}", 0)
            self._seen[f"pb-launch|{domain}"] = n + 1
            cid = "c" + hashlib.sha1(f"{domain}|{n}".encode()).hexdigest()[:12]
            self._containers[cid] = {
                "domain": domain,
                "polls_left": rng.randint(*self.faults.phantom_polls),
                "silent_empty": rng.random() < self.faults.phantom_silent_empty,
            }
        return _json(200, {"containerId": cid})

    def _pb_fetch(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            c = self._containers.get(request.url.params.get("id", ""))
            if c is None:
                return _json(404, {"error": "unknown container"})
            if c["polls_left"] > 0:
                c["polls_left"] -= 1
                return _json(200, {"status": "running"})
        return _json(200, {"status": "finished", "exitCode": 0})

    def _pb_result(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            c = self._containers.get(request.url.params.get("id", ""))
        if c is None:
            return _json(404, {"error": "unknown container"})
        if c["polls_left"] > 0:
            return _json(200, {"resultObject": None})
        people = [] if c["silent_empty"] else self._people(c["domain"], "phantombuster", persons_only=True)
        return _json(200, {"resultObject": json.dumps([{
            "firstName": p["first_name"], "lastName": p["last_name"], "title": p["title"],
            "email": p["email"], "companyDomain": p["company_domain"],
        } for p in people])})

    # --- Findymail ----------------------------------------------------------

    def _verify(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return _json(401, {"error": "unauthorized"})
        email = json.loads(request.content or b"{}").get("email", "")
        domain = email.rsplit("@", 1)[-1]
        known = next((p for p in self._by_domain.get(domain, []) if p["email"] == email), None)
        result = FINDYMAIL_RESULT[known["email_status"]] if known else "invalid"
        return _json(200, {"email": email, "verification": {"result": result}})
