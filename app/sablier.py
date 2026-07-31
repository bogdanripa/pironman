"""Durable Sablier scale-to-zero enrollment.

Sablier (a Traefik plugin already loaded on the proxy) stops idle app containers
and starts them on the next request. An app is enrolled by attaching the Sablier
middleware to its Traefik router and tagging the container with a Sablier group.

The trap this module exists to avoid: if that enrollment lives in Coolify's
auto-generated labels or a hand-edited proxy file, the next deploy regenerates it
away and the app silently stops sleeping. So instead we:

  1. read the app container's CURRENT labels straight off the running container
     (Coolify's complete generated set — no template to reconstruct),
  2. add the Sablier group labels + the plugin middleware, and prepend that
     middleware to every Traefik router's middleware chain, then
  3. write the whole set back as Coolify **read-only custom labels**, so Coolify
     stops regenerating them and the enrollment persists across every redeploy.

Enrollment keys on the STABLE app id (Sablier group = app id), never the
container name, which carries a per-deploy timestamp and would drift.

The control-plane app (SABLIER_EXCLUDE, default "api") is never enrolled — it runs
the ingester, auto-update sweep and alert loop and must never sleep.
"""
import json

from . import autoupdate, coolify
from .config import (SABLIER_URL, SABLIER_SESSION_DURATION, SABLIER_STRATEGY,
                     SABLIER_BLOCKING_TIMEOUT, SABLIER_EXCLUDE)


def excluded(app_id: str) -> bool:
    return app_id in SABLIER_EXCLUDE


def _mw(app_id: str) -> str:
    return f"sablier-{app_id}"


async def _current_labels(uuid: str) -> dict[str, str]:
    """The app container's live labels as a dict. Empty if there is no container
    yet (a freshly created app that has never deployed)."""
    name = await autoupdate._container_name(uuid)
    if not name:
        return {}
    rc, out = await autoupdate._docker(
        "inspect", "--format", "{{json .Config.Labels}}", name, timeout=30)
    if rc != 0:
        return {}
    try:
        return json.loads(out) or {}
    except ValueError:
        return {}


def _strip(labels: dict[str, str], app_id: str) -> dict[str, str]:
    """Remove this app's Sablier group labels, its plugin middleware definition,
    and any reference to that middleware in router chains — so enroll/unenroll
    start from a clean base."""
    mw = _mw(app_id)
    out: dict[str, str] = {}
    for k, v in labels.items():
        if k == "sablier.enable" or k == "sablier.group":
            continue
        if f".middlewares.{mw}." in k:
            continue
        if ".routers." in k and k.endswith(".middlewares"):
            v = ",".join(x for x in v.split(",") if x and x != mw)
            if not v:
                continue  # drop an empty chain rather than emit middlewares=
        out[k] = v
    return out


def enrolled_labels(labels: dict[str, str], app_id: str) -> dict[str, str]:
    """Return `labels` augmented with this app's Sablier enrollment."""
    d = _strip(labels, app_id)
    mw = _mw(app_id)

    # Discovery: Sablier's docker provider finds the container by this group.
    d["sablier.enable"] = "true"
    d["sablier.group"] = app_id

    # The Traefik plugin middleware that fronts the router.
    p = f"traefik.http.middlewares.{mw}.plugin.sablier"
    d[f"{p}.sablierUrl"] = SABLIER_URL
    d[f"{p}.group"] = app_id
    d[f"{p}.sessionDuration"] = SABLIER_SESSION_DURATION
    # Blocking (the default): the caller's request is held until the app answers,
    # so waking looks like one slow request rather than an HTML interstitial.
    if SABLIER_STRATEGY == "dynamic":
        d[f"{p}.dynamic.displayName"] = app_id
    else:
        d[f"{p}.blocking.timeout"] = SABLIER_BLOCKING_TIMEOUT

    # Prepend the middleware to every existing router chain (Coolify always emits
    # a .middlewares label per router — gzip — so there is one to prepend to).
    for k in list(d):
        if ".routers." in k and k.endswith(".middlewares"):
            chain = [x for x in d[k].split(",") if x]
            if mw not in chain:
                d[k] = ",".join([mw] + chain)
    return d


def _as_list(d: dict[str, str]) -> list[str]:
    return [f"{k}={v}" for k, v in d.items()]


async def enroll(uuid: str, app_id: str) -> None:
    """Write the app's Sablier enrollment into Coolify (read-only labels) and
    redeploy so it takes effect. Raises if the app has no running container to
    read the base labels from (deploy it once first)."""
    labels = await _current_labels(uuid)
    if not labels:
        raise RuntimeError(
            "no running container to read labels from — deploy the app once first")
    await coolify.set_custom_labels(uuid, _as_list(enrolled_labels(labels, app_id)))
    await coolify.deploy(uuid)


async def host_uptime_seconds() -> float | None:
    """The HOST's uptime. /proc/uptime is not namespaced, so a container reads the
    machine's, which is how we tell a reboot from an ordinary restart of this
    container (which happens on every self-deploy)."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


async def stop_sleeping_apps(conn) -> dict:
    """Stop every enrolled, sleep-enabled app that is currently running.

    Called once after a host reboot. Docker's restart policy brings containers
    back when the daemon starts — including ones Sablier had stopped — so a box
    that reboots overnight wakes every app and holds that memory until each idles
    out again. Apps that scale to zero should come back asleep and start on
    demand, which is the whole point of enrolling them.

    Only apps we know Sablier is managing (sablier_enrolled) are touched, so an
    app that would have no way to be woken again is never stopped.
    """
    rows = await conn.fetch(
        "SELECT id, coolify_uuid FROM apps "
        "WHERE sleep_when_idle = true AND sablier_enrolled = true "
        "AND coolify_uuid IS NOT NULL ORDER BY id")
    stopped = []
    for row in rows:
        if excluded(row["id"]):
            continue
        name = await autoupdate._container_name(row["coolify_uuid"])
        if not name:
            continue  # already down
        rc, _ = await autoupdate._docker("stop", name, timeout=60)
        if rc == 0:
            stopped.append(row["id"])
    return {"stopped": stopped}


async def unenroll(uuid: str, app_id: str) -> None:
    """Remove the app's Sablier enrollment and redeploy. Leaves the labels
    read-only (Coolify keeps managing the rest verbatim)."""
    labels = await _current_labels(uuid)
    if not labels:
        return
    await coolify.set_custom_labels(uuid, _as_list(_strip(labels, app_id)))
    await coolify.deploy(uuid)
