"""Keeping one visitor from spending everyone's SEC request budget.

## The threat this exists for

`core.py` already holds one process-wide `RateLimiter` at 4 req/s, and that is
what keeps *SEC* happy: however many people are on the page, SEC sees one
requester under its ten-per-second ceiling. What it does not do is decide **whose**
requests those are. A single script pointed at the public proxy saturates the
budget, and every other visitor then waits behind it — the limiter does not drop
their requests, it queues them, so the failure mode is a page that appears to
hang rather than one that says it is busy.

So this module is not about protecting SEC. SEC is already protected. It is about
protecting the *other visitors* from one of them, and about failing loudly instead
of slowly when that happens.

## What it does

Two controls, both deliberately small:

* **A per-client sliding window.** N requests per window, keyed on the client
  address. Over the limit is a 429 with `Retry-After`, which the app already
  knows how to display — `throttled` is an existing error kind with its own copy.
* **A global in-flight cap.** `ThreadingHTTPServer` spawns a thread per
  connection; a flood otherwise turns into hundreds of threads all blocked in the
  rate limiter's `sleep`. Past the cap the answer is an immediate 503, which is
  cheap, rather than a thread that is expensive.

## What it does NOT prevent, stated plainly

* **It is not authentication.** There is no account, no key, and no way to tell a
  second visitor from a second tab.
* **A distributed client defeats it.** Requests from many addresses are, to this
  code, many visitors. The global cap and the SEC limiter are what remain, and
  what remains is "the proxy stays up and SEC stays unbothered", not "the service
  stays responsive".
* **The client address may not be the client.** Behind a hosting provider's load
  balancer the socket peer is the balancer, so the address has to come from
  `X-Forwarded-For` — a header the client itself can send. It is trusted only
  when `SEC_TABLES_TRUST_FORWARDED` is set, which is correct exactly when the
  service is genuinely behind a proxy that overwrites it. Set it wrong in either
  direction and the window keys on the wrong thing: every visitor shares one
  bucket (untrusted, behind a balancer), or a visitor picks their own bucket per
  request (trusted, directly exposed).
* **Cache hits are counted too.** Charging only the requests that reach SEC would
  be a better measure of cost, but it would also mean the limit could not be
  checked until after the work was done. Counting requests is the version that
  can refuse before spending anything.

None of the above is a reason not to have it. It converts the common accident —
someone's loop, a stuck retry, a crawler — from a service-wide stall into a
clear per-client refusal, and that is the whole claim.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

# One visitor doing the intended thing spends roughly three requests per filing
# examined (list, fetch, and a second fetch if they change their mind about
# which filing). Sixty in five minutes is about twenty filings — far more than a
# session, far less than a script.
DEFAULT_REQUESTS = 60
DEFAULT_WINDOW_SECONDS = 300.0

# Threads, not requests: each one may sit in the shared 4 req/s limiter, so this
# is the depth of that queue. Past it, waiting is worse than being told no.
DEFAULT_MAX_IN_FLIGHT = 8


@dataclass
class SlidingWindow:
    """Per-client request counts over a moving window.

    A deque of timestamps rather than a token bucket because the useful answer
    here is "how long until you may try again", and a bucket has to reconstruct
    that from a refill rate while the deque simply reads its oldest entry.
    """

    limit: int = DEFAULT_REQUESTS
    window_seconds: float = DEFAULT_WINDOW_SECONDS
    _hits: dict[str, deque[float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def check(self, key: str, now: Optional[float] = None) -> tuple[bool, int, int]:
        """Record one request. Returns (allowed, remaining, retry_after_seconds).

        The request is recorded only when it is allowed. Counting refused
        requests would let a client that is already over the limit hold itself
        over it indefinitely by continuing to knock.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = deque()
                self._hits[key] = hits
            cutoff = now - self.window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                retry = max(1, int(hits[0] + self.window_seconds - now) + 1)
                return False, 0, retry
            hits.append(now)
            # Opportunistic sweep: without it, one request each from many
            # addresses leaves an empty deque per address forever. Bounded to the
            # request being served so it never becomes a long pause.
            if len(self._hits) > 4096:
                self._sweep(cutoff)
            return True, self.limit - len(hits), 0

    def _sweep(self, cutoff: float) -> None:
        """Drop clients with nothing left in the window. Caller holds the lock."""
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            del self._hits[key]

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


class InFlight:
    """A counter with a ceiling, used as a context manager.

    `acquire()` returns False rather than blocking: the point is to refuse
    quickly, and a blocking acquire would recreate the queue it exists to
    prevent.
    """

    def __init__(self, limit: int = DEFAULT_MAX_IN_FLIGHT) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._count = 0
        self.peak = 0

    def acquire(self) -> bool:
        with self._lock:
            if self._count >= self.limit:
                return False
            self._count += 1
            self.peak = max(self.peak, self._count)
            return True

    def release(self) -> None:
        with self._lock:
            self._count = max(0, self._count - 1)

    @property
    def current(self) -> int:
        with self._lock:
            return self._count


@dataclass
class AbuseGuard:
    """Both controls, and the client-identification decision behind them."""

    window: SlidingWindow = field(default_factory=SlidingWindow)
    in_flight: InFlight = field(default_factory=InFlight)
    trust_forwarded: bool = False

    def client_key(self, peer_address: str, forwarded_for: Optional[str]) -> str:
        """Who this request is attributed to.

        `X-Forwarded-For` is a comma-separated chain appended to by each hop, so
        the client is the *first* entry — but only if every hop after it is
        trusted, because a client that sends its own header puts itself first.
        Hence the flag: when the service is not behind a proxy that overwrites
        the header, the socket peer is the only thing that cannot be forged.
        """
        if self.trust_forwarded and forwarded_for:
            first = forwarded_for.split(",")[0].strip()
            if first:
                return first
        return peer_address

    def check(self, key: str) -> tuple[bool, int, int]:
        return self.window.check(key)


def guard_from_env(env: Optional[dict[str, str]] = None) -> AbuseGuard:
    env = os.environ if env is None else env

    def _int(name: str, default: int) -> int:
        try:
            return max(1, int(env.get(name, "") or default))
        except ValueError:
            return default

    def _float(name: str, default: float) -> float:
        try:
            return max(1.0, float(env.get(name, "") or default))
        except ValueError:
            return default

    return AbuseGuard(
        window=SlidingWindow(
            limit=_int("SEC_TABLES_RATE_LIMIT", DEFAULT_REQUESTS),
            window_seconds=_float("SEC_TABLES_RATE_WINDOW", DEFAULT_WINDOW_SECONDS),
        ),
        in_flight=InFlight(_int("SEC_TABLES_MAX_IN_FLIGHT", DEFAULT_MAX_IN_FLIGHT)),
        trust_forwarded=(
            env.get("SEC_TABLES_TRUST_FORWARDED", "").strip().lower() in {"1", "true", "yes"}
        ),
    )
