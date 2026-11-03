"""Is the app actually serving? — the question neither earlier check asked.

Two outages, both multi-hour, both found by a person rather than an alert:

  * a container was deleted by a false-negative healthcheck rollback. The app
    was scale-to-zero, so "no container" read as "asleep" and nothing fired.
  * Traefik restarted on a reboot, failed to download its Sablier plugin before
    the network was ready, and carried on without it. Every router referencing
    the plugin's middleware became invalid, so every scale-to-zero app was
    unreachable — while its container sat there, running and healthy.

The second one is the instructive one: the fix written for the first (does a
container exist?) passed cleanly throughout. Every *internal* signal was green.
The only thing wrong was the thing nobody measured — a request to the public URL
did not come back.

The 5xx rate was supposed to cover this, and cannot: 5xx only exist if somebody
is making requests, so a quiet app with a dead route produces no evidence at all.
The quieter the app, the longer it stays broken. This module inverts that.

Two checks, deliberately different in kind:

  * router health reads Traefik's own view of its configuration and costs
    nothing — it wakes nothing and catches a whole family of faults (invalid
    middleware, missing service, a plugin that did not load) within minutes;
  * a synthetic fetch of the public URL is the only CAUSE-INDEPENDENT check
    here. It cannot tell you why, only that a real request did not come back,
    which is the one thing that matters and the one thing we twice failed to
    notice. It costs a wake, so it runs once a day per app.
"""
import hashlib
from datetime import date, datetime

import httpx

# A response at all means the edge reached the app: routing works, which is what
# this measures. 401/403/404 are the app answering — not our business here.
# These three are the edge saying it could not get there, and are exactly what
# both outages produced (pironman-web's "backend has no route" is a 503).
UNREACHABLE_STATUSES = frozenset({502, 503, 504})

# A cold scale-to-zero app is woken by this request, and Sablier blocks the
# request while the container starts. Must exceed its blocking timeout (60s) or
# a slow but healthy wake reads as an outage.
PROBE_TIMEOUT = 75.0

# Attempts before an app is called unreachable. A once-a-day check that pages on
# a single dropped connection is a check people learn to ignore.
PROBE_ATTEMPTS = 2


def probe_hour(app_id: str) -> int:
    """Which hour (UTC) this app is probed in — stable, derived from its id.

    Spread, so the whole estate is not woken at once. Deterministic rather than
    random per tick: rolling a die every hour gives roughly-daily with skipped
    days and occasional doubles, and a skipped day is a day of blindness. This
    gives each app exactly one slot and keeps it.
    """
    digest = hashlib.sha1(app_id.encode()).digest()
    return digest[0] % 24


def due_for_probe(app_id: str, now: datetime, probed_on: date | None) -> bool:
    """True in the app's own hour, at most once per day."""
    if now.hour != probe_hour(app_id):
        return False
    return probed_on != now.date()


def is_serving(status: int | None) -> bool:
    """Did a request reach the app? None means no HTTP response at all."""
    if status is None:
        return False
    return status not in UNREACHABLE_STATUSES


async def probe(url: str, client: httpx.AsyncClient | None = None) -> tuple[bool, int | None]:
    """Fetch the app's public URL through the edge. -> (serving, last status)

    Deliberately the PUBLIC url, not the container: the whole point is to travel
    the path a real user travels — DNS, Cloudflare, Traefik, the Sablier
    middleware, the wake, the container. Every one of those broke at some point
    without an internal check noticing.
    """
    own = client is None
    client = client or httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=False)
    status: int | None = None
    try:
        for _ in range(PROBE_ATTEMPTS):
            try:
                status = (await client.get(url)).status_code
            except Exception:
                status = None
            if is_serving(status):
                return True, status
        return False, status
    finally:
        if own:
            await client.aclose()


def reachability_decision(
    aid: str, serving: bool, status: int | None, url: str, prev: dict | None,
) -> tuple[list[str], bool]:
    """Alert on an app that did not answer. -> (messages, latch)

    Latched, so a broken app is reported once rather than daily, and cleared on
    recovery so the next break is reported again.
    """
    alerted = bool(prev.get("alerted_unreachable")) if prev else False
    messages: list[str] = []

    if not serving and not alerted:
        detail = f"HTTP {status}" if status is not None else "no response"
        messages.append(
            f"\U0001f534 <b>{aid}</b> is NOT serving — a request to its public "
            f"URL returned {detail}. The container may look healthy; this is the "
            f"path a real user takes.\n{url}")
        alerted = True
    elif serving and alerted:
        messages.append(f"\U0001f7e2 <b>{aid}</b> is serving again\n{url}")
        alerted = False

    return messages, alerted


def router_problems(routers: list[dict]) -> dict[str, str]:
    """Traefik routers that are not usable. -> {router name: why}

    Traefik keeps a router in its configuration and marks it disabled when it
    cannot build it — the shape of the Sablier outage, where every router
    referencing the plugin's middleware was rejected with "invalid middleware
    type or middleware does not exist" while the containers stayed healthy.

    Read from Traefik rather than inferred from logs: this is its own account of
    what it will and will not serve, and it does not change wording between
    versions the way a log line does.
    """
    problems: dict[str, str] = {}
    for r in routers:
        name = r.get("name") or "?"
        if r.get("status") == "enabled":
            continue
        why = "; ".join(r["error"]) if isinstance(r.get("error"), list) and r["error"] \
            else (r.get("status") or "not enabled")
        problems[name] = why
    return problems


async def fetch_routers(api_url: str, client: httpx.AsyncClient | None = None) -> list[dict] | None:
    """Traefik's router table, or None if the API could not be read.

    None is "cannot tell", not "all well": the caller reports it rather than
    treating silence as health. A check that quietly stops checking is how both
    of the outages above lasted as long as they did.
    """
    own = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        res = await client.get(f"{api_url.rstrip('/')}/api/http/routers")
        if res.status_code != 200:
            return None
        data = res.json()
        return data if isinstance(data, list) else None
    except Exception:
        return None
    finally:
        if own:
            await client.aclose()
