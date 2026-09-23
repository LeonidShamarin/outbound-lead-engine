"""One place that talks HTTP to providers: bounded retries and a cost ledger.

Retry here is about transport only (429, 5xx, network). A 4xx other than 429 is
the caller's bug and is raised at once: retrying a bad request just pays for the
same error again.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status = status


@dataclass
class CallRecord:
    provider: str
    endpoint: str
    http_status: int | None
    cost_usd: float = 0.0
    results: int = 0


@dataclass
class Ledger:
    """Every call made while enriching one company. Written to provider_calls."""

    # A company needs a few dozen calls at most. The cap turns a runaway loop in a
    # client into an error instead of an ever-growing list.
    max_records: int = 2000
    records: list[CallRecord] = field(default_factory=list)

    def record(self, provider: str, endpoint: str, http_status: int | None, cost_usd: float = 0.0) -> CallRecord:
        if len(self.records) >= self.max_records:
            raise ProviderError(provider, f"more than {self.max_records} calls for one company, stopping")
        rec = CallRecord(provider, endpoint, http_status, cost_usd)
        self.records.append(rec)
        return rec

    @property
    def cost_usd(self) -> float:
        return round(sum(r.cost_usd for r in self.records), 4)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0

    def delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), self.max_delay_s)
            except ValueError:
                pass  # an HTTP date or garbage: fall back to backoff
        return min(self.base_delay_s * 2 ** (attempt - 1), self.max_delay_s)


def send(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    provider: str,
    endpoint: str,
    ledger: Ledger,
    policy: RetryPolicy = RetryPolicy(),
    sleep: Callable[[float], None] = time.sleep,
    cost_usd: float = 0.0,
    **kwargs,
) -> tuple[httpx.Response, CallRecord]:
    """Send with bounded retries. Only a 2xx/3xx answer is billed.

    Returns the response and its ledger record, so a caller that bills per result
    (PhantomBuster charges per lead) can set the cost after parsing.
    """
    last_error = "no attempt made"
    status: int | None = None
    for attempt in range(1, policy.max_attempts + 1):
        retry_after = None
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.TransportError as e:
            ledger.record(provider, endpoint, None)
            status, last_error = None, f"network error: {e.__class__.__name__}"
        else:
            status = resp.status_code
            if status < 400:
                return resp, ledger.record(provider, endpoint, status, cost_usd)
            ledger.record(provider, endpoint, status)
            if status != 429 and status < 500:
                raise ProviderError(provider, f"{endpoint} answered {status}", status)
            last_error = f"{endpoint} answered {status}"
            retry_after = resp.headers.get("Retry-After")
        if attempt < policy.max_attempts:
            sleep(policy.delay(attempt, retry_after))
    raise ProviderError(provider, f"{last_error} after {policy.max_attempts} attempts", status)
