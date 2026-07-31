"""Cross-origin policy for a proxy that no longer sits on the page's origin.

Until this module existed the browser and the proxy shared an origin — the dev
static server forwarded `/api/*` — so CORS never came up. Deploying the frontend
to GitHub Pages and the proxy somewhere else makes every API call cross-origin,
and three things then have to be got right rather than assumed:

1. **An allowlist, never `*`.** `Access-Control-Allow-Origin: *` would be
   *technically* harmless here — there are no cookies, no `Authorization`, no
   ambient credentials, so a wildcard grants a stranger's page nothing it could
   not get with `curl`. It is still refused, for a reason that is about cost
   rather than confidentiality: this server holds one shared SEC request budget,
   and a wildcard is a standing invitation to embed it in someone else's page.
   The allowlist does not *prevent* that (see below) — it removes the easy path.

2. **`Vary: Origin` on everything.** The response body for `/api/filing` is a
   filing, identical for every origin, so it is exactly the kind of thing a CDN
   or a corporate proxy will cache. Cache one origin's `Access-Control-Allow-
   Origin` and replay it to another and the app breaks for the second visitor in
   a way nobody can reproduce. The header is emitted on allowed *and* denied
   responses, because the denial varies by origin too.

3. **`Access-Control-Expose-Headers`.** A cross-origin `fetch()` can read only
   seven response headers by default, and `X-Filing-Meta` — which carries the
   filing's date, form, CIK and source URL — is not one of them. Without this the
   filing bytes arrive and `parseFilingMeta` throws on `null`, which is a
   confusing way to discover a CORS rule.

**What this is not.** CORS is enforced by browsers, on behalf of the *user*, to
stop one site reading another's authenticated data. It is not authentication and
not access control: `curl`, a script, or any non-browser client sends whatever
`Origin` it likes or none at all, and gets the same answer. Abuse protection is
therefore a separate mechanism — see `limits.py` — and this file must never be
described as securing the proxy.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

# The deployed frontend. A GitHub Pages *project* site serves from the owner's
# domain, so the origin is the account, not the repository — everything under
# `ruturaj-vasant.github.io` shares it, and there is no narrower origin to name.
PAGES_ORIGIN = "https://ruturaj-vasant.github.io"

# Loopback, any port. The dev server picks 5199, the browser suite may pick
# another, and a contributor running a bundler gets a third. Pinning one port
# would mean the deployed proxy could only be developed against from one setup;
# pinning none would mean `*`. Note this makes any page a visitor is running
# locally able to call the proxy — which is the same access `curl` already has.
LOOPBACK_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?$")

# What the browser is allowed to *read* off a cross-origin response.
EXPOSED_HEADERS = ("X-Filing-Meta", "X-Filing-Cache", "X-RateLimit-Remaining", "Retry-After")

# What the browser is allowed to *send*. `Content-Type: application/json` is not
# a CORS-safelisted value, so this is what makes the preflight succeed at all.
ALLOWED_REQUEST_HEADERS = ("Content-Type",)

ALLOWED_METHODS = ("POST", "GET", "OPTIONS")

# Chromium caps this at 2 hours regardless of what is sent; asking for a day
# would simply be ignored. Two hours removes the preflight from every call after
# the first without making a policy change take a day to propagate.
PREFLIGHT_MAX_AGE = 7200


@dataclass(frozen=True)
class CorsPolicy:
    """Which origins may read this proxy's responses.

    `allow_loopback` is separate from `origins` so that a deployment can turn
    local development off without having to enumerate every port it would
    otherwise have to allow.
    """

    origins: frozenset[str] = field(default_factory=lambda: frozenset({PAGES_ORIGIN}))
    allow_loopback: bool = True

    def allows(self, origin: Optional[str]) -> bool:
        """True when `origin` may read a response.

        A missing `Origin` is not an allowed origin — it is *no* origin, which is
        what a non-browser client sends. Callers distinguish the two: a request
        with no `Origin` is served (there is no browser to protect and nothing to
        echo), a request with a disallowed one is refused.
        """
        if not origin:
            return False
        if origin in self.origins:
            return True
        return self.allow_loopback and bool(LOOPBACK_RE.match(origin))

    def headers(self, origin: Optional[str], *, preflight: bool = False) -> dict[str, str]:
        """The CORS headers for one response.

        `Vary: Origin` is present whether or not the origin was allowed, because
        the *decision* varies by origin and a shared cache must not reuse either
        answer for a different one.
        """
        out = {"Vary": "Origin"}
        if not self.allows(origin):
            return out
        out["Access-Control-Allow-Origin"] = origin  # type: ignore[assignment]
        if preflight:
            out["Access-Control-Allow-Methods"] = ", ".join(ALLOWED_METHODS)
            out["Access-Control-Allow-Headers"] = ", ".join(ALLOWED_REQUEST_HEADERS)
            out["Access-Control-Max-Age"] = str(PREFLIGHT_MAX_AGE)
        else:
            out["Access-Control-Expose-Headers"] = ", ".join(EXPOSED_HEADERS)
        # Deliberately absent: `Access-Control-Allow-Credentials`. Nothing here
        # uses a cookie or an `Authorization` header, and sending it would let a
        # future change start carrying ambient credentials cross-origin without
        # anyone revisiting this file.
        return out


def parse_origins(raw: Optional[str]) -> frozenset[str]:
    """Comma-separated origins from configuration, normalised.

    Trailing slashes are stripped because `https://example.com/` is a URL and
    `https://example.com` is the origin — a browser sends the latter, and an
    allowlist holding the former silently matches nothing.
    """
    if not raw:
        return frozenset()
    out = set()
    for part in raw.split(","):
        candidate = part.strip().rstrip("/")
        if candidate:
            out.add(candidate)
    return frozenset(out)


def policy_from_env(env: Optional[dict[str, str]] = None) -> CorsPolicy:
    """The policy a deployment actually runs.

    `SEC_TABLES_ALLOWED_ORIGINS` *replaces* the default rather than extending it,
    so a fork deploying its own Pages site does not silently keep this one's
    origin on the list.
    """
    env = os.environ if env is None else env
    configured = parse_origins(env.get("SEC_TABLES_ALLOWED_ORIGINS"))
    loopback = env.get("SEC_TABLES_ALLOW_LOOPBACK", "1").strip().lower() not in {"0", "false", "no"}
    return CorsPolicy(
        origins=configured or frozenset({PAGES_ORIGIN}),
        allow_loopback=loopback,
    )
