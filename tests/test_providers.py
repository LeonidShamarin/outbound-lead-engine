"""Provider clients against the mock: shapes, billing, retries, tokens, polling. No database."""

from __future__ import annotations

import httpx
import pytest
from helpers import http_for, person, setup, world

from leadengine.normalize import seniority_from_title
from leadengine.providers.clients import PRICES_USD, PhantomBusterClient, SnovClient
from leadengine.providers.http import Ledger, ProviderError, RetryPolicy, send
from leadengine.providers.mock import Faults, MockProviders

D = "acme.example"


def _by_name(clients, name):
    return next(p for p in clients.providers if p.name == name)


def test_apollo_search_is_free_and_each_reveal_is_billed() -> None:
    w = world(*(person(D, f"P{i}", "Novak", apollo=True) for i in range(3)))
    _, clients, _, _ = setup(w)
    ledger = Ledger()
    found = _by_name(clients, "apollo").find(D, set(), ledger)

    assert sorted(c.email for c in found) == sorted(p["email"] for p in w["people"])
    assert all("not_unlocked" not in c.email for c in found)
    search = [r for r in ledger.records if r.endpoint == "people_search"]
    match = [r for r in ledger.records if r.endpoint == "people_match"]
    assert [r.cost_usd for r in search] == [0.0]
    assert len(match) == 3 and ledger.cost_usd == pytest.approx(3 * PRICES_USD["apollo_match"])


def test_apollo_does_not_pay_to_reveal_a_person_already_known() -> None:
    w = world(person(D, "Anna", "Novak", apollo=True), person(D, "Marek", "Keller", apollo=True))
    _, clients, _, _ = setup(w)
    ledger = Ledger()
    found = _by_name(clients, "apollo").find(D, {"anna|novak"}, ledger)
    assert [c.first_name for c in found] == ["Marek"]
    assert sum(r.endpoint == "people_match" for r in ledger.records) == 1


def test_apollo_pages_through_long_lists() -> None:
    w = world(*(person(D, f"P{i:02d}", "Berg", apollo=True) for i in range(60)))
    _, clients, _, _ = setup(w)
    ledger = Ledger()
    found = _by_name(clients, "apollo").find(D, set(), ledger)
    assert len(found) == 60
    assert sum(r.endpoint == "people_search" for r in ledger.records) == 3  # 25 + 25 + 10


def test_hunter_pages_with_offset_and_flags_role_mailboxes() -> None:
    people = [person(D, f"P{i:02d}", "Hart", hunter=True) for i in range(30)]
    people.append(person(D, None, None, email=f"info@{D}", role=True, hunter=True))
    _, clients, _, _ = setup(world(*people))
    ledger = Ledger()
    found = _by_name(clients, "hunter").find(D, set(), ledger)
    assert len(found) == 31
    assert [c.email for c in found if c.role] == [f"info@{D}"]
    assert sum(r.endpoint == "domain_search" for r in ledger.records) == 2


def test_429_is_retried_after_retry_after_and_nothing_is_lost() -> None:
    w = world(*(person(D, f"P{i}", "Visser", hunter=True) for i in range(4)))
    faults = Faults(script={"hunter/v2/domain-search": [429, 429]}, retry_after_s=7)
    _, clients, slept, _ = setup(w, faults)
    ledger = Ledger()
    found = _by_name(clients, "hunter").find(D, set(), ledger)
    assert len(found) == 4
    assert slept == [7, 7]
    statuses = [r.http_status for r in ledger.records]
    assert statuses == [429, 429, 200]
    assert ledger.cost_usd == pytest.approx(PRICES_USD["hunter_search"])  # failed attempts are not billed


def test_5xx_retries_are_bounded_and_then_raise() -> None:
    w = world(person(D, "A", "B", hunter=True))
    _, clients, slept, _ = setup(w, Faults(rate_5xx=1.0), max_attempts=3)
    ledger = Ledger()
    with pytest.raises(ProviderError) as e:
        _by_name(clients, "hunter").find(D, set(), ledger)
    assert e.value.status == 503
    assert len(ledger.records) == 3 and len(slept) == 2
    assert slept == [1.0, 2.0]  # exponential backoff when there is no Retry-After


def test_a_client_error_is_not_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(400, json={"error": "bad"})

    ledger = Ledger()
    with pytest.raises(ProviderError) as e:
        send(http_for(handler), "GET", "http://x/y", provider="hunter", endpoint="domain_search",
             ledger=ledger, sleep=lambda _: None)
    assert e.value.status == 400 and len(calls) == 1


def test_retry_after_is_capped() -> None:
    assert RetryPolicy(max_delay_s=30).delay(1, "3600") == 30
    assert RetryPolicy().delay(2, "not-a-number") == 2.0


def test_snov_token_is_reused_within_the_hour_and_refreshed_after() -> None:
    w = world(person(D, "A", "B", snov=True), person("other.example", "C", "D", snov=True))
    mock, clients, _, clock = setup(w)
    snov = _by_name(clients, "snov")
    snov.find(D, set(), Ledger())
    clock.now += 49 * 60
    snov.find("other.example", set(), Ledger())
    assert mock.count("snov/v1/oauth") == 1
    clock.now += 2 * 60
    snov.find(D, set(), Ledger())
    assert mock.count("snov/v1/oauth") == 2


def test_snov_refreshes_once_when_the_token_is_rejected() -> None:
    w = world(person(D, "A", "B", snov=True))
    mock, clients, _, _ = setup(w)
    snov = _by_name(clients, "snov")
    snov._token, snov._token_at = "snov-stolen", snov.clock()  # looks fresh, server does not know it
    found = snov.find(D, set(), Ledger())
    assert len(found) == 1 and mock.count("snov/v1/oauth") == 1


def test_snov_stops_when_the_cursor_does_not_advance() -> None:
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("access_token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        pages.append(1)
        emails = [{"email": f"p{i}@{D}", "firstName": "P", "lastName": str(i), "type": "personal"} for i in range(25)]
        return httpx.Response(200, json={"emails": emails, "lastId": 25})  # a buggy API: same cursor forever

    snov = SnovClient(http_for(handler), "http://snov", "id", client_secret="s", sleep=lambda _: None)
    found = snov.find(D, set(), Ledger())
    assert len(pages) == 2  # second page returned the same cursor, loop stopped
    assert len(found) == 50


def test_phantombuster_polls_until_finished() -> None:
    w = world(person(D, "A", "B", phantombuster=True), person(D, "C", "D", phantombuster=True))
    _, clients, slept, _ = setup(w, Faults(phantom_polls=(3, 3)))
    ledger = Ledger()
    found = _by_name(clients, "phantombuster").find(D, set(), ledger)
    assert len(found) == 2
    assert slept == [30.0, 30.0, 30.0]
    assert ledger.cost_usd == pytest.approx(2 * PRICES_USD["phantombuster_lead"])


def test_phantombuster_gives_up_after_max_polls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("launch"):
            return httpx.Response(200, json={"containerId": "c1"})
        return httpx.Response(200, json={"status": "running"})

    slept: list[float] = []
    pb = PhantomBusterClient(http_for(handler), "http://pb", "k", max_polls=5, sleep=slept.append)
    with pytest.raises(ProviderError, match="not finished after 5 polls"):
        pb.find(D, set(), Ledger())
    assert len(slept) == 5


def test_phantombuster_silent_empty_run_is_counted() -> None:
    w = world(person(D, "A", "B", phantombuster=True))
    _, clients, _, _ = setup(w, Faults(phantom_silent_empty=1.0))
    pb = _by_name(clients, "phantombuster")
    assert pb.find(D, set(), Ledger()) == []
    assert pb.empty_runs == 1


def test_findymail_maps_statuses_and_unknown_is_invalid() -> None:
    w = world(person(D, "A", "B", status="valid"), person(D, "C", "D", status="risky"),
              person(D, "E", "F", status="invalid"))
    _, clients, _, _ = setup(w)
    ledger = Ledger()
    got = [clients.verifier.verify(p["email"], ledger) for p in w["people"]]
    assert got == ["verified", "risky", "invalid"]
    assert clients.verifier.verify(f"nobody@{D}", ledger) == "invalid"


def test_injected_faults_do_not_depend_on_request_order() -> None:
    domains = [f"d{i}.example" for i in range(40)]
    w = world(*(person(d, "A", "B", hunter=True) for d in domains))

    def outcomes(order: list[str]) -> dict[str, int]:
        mock = MockProviders(w, Faults(rate_429=0.3, rate_5xx=0.1), seed=7)
        http = http_for(mock.handle)
        return {d: http.get("http://m/hunter/v2/domain-search", params={"domain": d, "api_key": "k"}).status_code
                for d in order}

    forward = outcomes(domains)
    backward = outcomes(domains[::-1])
    assert forward == backward
    assert set(forward.values()) == {200, 429, 503}  # the rates really produced all three


def test_ledger_cap_stops_a_runaway_client() -> None:
    ledger = Ledger(max_records=3)
    for _ in range(3):
        ledger.record("hunter", "x", 200)
    with pytest.raises(ProviderError, match="more than 3 calls"):
        ledger.record("hunter", "x", 200)


@pytest.mark.parametrize("title,expected", [
    ("Chief Revenue Officer", "c_level"), ("VP Marketing", "vp"), ("Head of Growth", "director"),
    ("Event Manager", "manager"), ("SDR", "ic"), ("", None), (None, None),
])
def test_seniority_from_title(title, expected) -> None:
    assert seniority_from_title(title) == expected
