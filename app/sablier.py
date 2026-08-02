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
                     SABLIER_BLOCKING_TIMEOUT, SABLIER_EXCLUDE, STATIC_HOST_APP)


class NoContainer(RuntimeError):
    """The app has never deployed, so there are no base labels to build on."""


def excluded(app_id: str) -> bool:
    """Apps that must never sleep. The static host is hard-coded rather than left
    to SABLIER_EXCLUDE: it is the thing that wakes everything else, so if it ever
    slept, every app behind it would be unreachable and nothing could start it."""
    return app_id in SABLIER_EXCLUDE or app_id == STATIC_HOST_APP


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


# The enrollment and the static host's routing marker are written together, in
# routing.apply_backend_labels — see the note there on why they cannot be two
# separate read-modify-writes. This module supplies the two pure transforms.
stripped = _strip


def is_enrolled(labels: dict[str, str], app_id: str) -> bool:
    """Whether these labels actually carry this app's enrollment — the Sablier
    group Sablier discovers it by, and the middleware that refreshes its session."""
    mw = f"traefik.http.middlewares.{_mw(app_id)}."
    return (labels.get("sablier.group") == app_id
            and any(k.startswith(mw) for k in labels))


async def reconcile() -> list[dict]:
    """Re-enroll any app whose Sablier labels have gone missing, and recreate any
    whose container has gone missing entirely.

    The enrollment is written as Coolify custom labels marked read-only, but this
    Coolify build rejects the read-only flag, so a deploy can regenerate the label
    block and drop the middleware. Nothing would notice: `sablier_enrolled` stays
    true in the database, the app simply stops sleeping and runs for ever. This
    compares the database's belief against the container's actual labels — the
    same shape of drift repair the static host's routers already get.
    """
    from .db import pool
    out: list[dict] = []
    async with pool().acquire() as c:
        apps = await c.fetch(
            "SELECT id, coolify_uuid, has_frontend, redirects, sleep_when_idle "
            "FROM apps WHERE sleep_when_idle = true AND coolify_uuid IS NOT NULL "
            "ORDER BY id")
        # Excluded apps first, and checked whatever their row says. `excluded()`
        # is what exempts them, not the sleep_when_idle column, so an enrollment
        # written while the column said otherwise outlives any change to it.
        #
        # This pass exists because skipping was not enough. `api` was enrolled by
        # the _DEFRONTED repair, and nothing here would have taken it back off:
        # the loop below skipped it for being excluded, and the _DEFRONTED pass
        # only touches apps whose rule carries the static-host marker, which the
        # control plane's does not. Prevention without repair leaves the app
        # already broken broken for ever, which is the state this was written in.
        for app in await c.fetch(
                "SELECT id, coolify_uuid FROM apps "
                "WHERE coolify_uuid IS NOT NULL ORDER BY id"):
            if not excluded(app["id"]):
                continue
            try:
                labels = await _current_labels(app["coolify_uuid"])
                if not labels or not is_enrolled(labels, app["id"]):
                    continue
                from . import routing  # lazy: routing imports this module
                if await routing.apply_backend_labels(
                        app["id"], app["coolify_uuid"], sleeps=False,
                        fronted=False):
                    out.append({"id": app["id"], "unenrolled": True,
                                "reason": "must never sleep, but was enrolled"})
            except Exception as e:
                out.append({"id": app["id"], "error": repr(e)})

        for app in apps:
            if excluded(app["id"]):
                continue  # handled by the pass above
            try:
                # A sleeping app is SUPPOSED to have a stopped container, so the
                # check that matters is existence, not state. `_container_name`
                # lists stopped ones too (docker ps -a), so None here means the
                # container is not stopped — it is gone.
                #
                # That state is terminal without help. Sablier discovers an app
                # through the `sablier.group` label, which lives on the container:
                # once the container is deleted the group has no members, so the
                # wake request 404s ("Group not found"), the name-based fallback
                # 500s ("No such container"), and every request to the app is
                # 502/503 for ever. Scale-to-zero becomes scale-to-zero-and-stay.
                # Only a deploy can recreate a container, so ask for one — and
                # leave re-enrollment to the next pass, since the labels this
                # reconciles are read off a container that does not exist yet.
                if not await autoupdate._container_name(app["coolify_uuid"]):
                    await coolify.deploy(
                        app["coolify_uuid"], app_id=app["id"],
                        reason="sablier reconcile: the container was gone, so "
                               "the app could never wake — redeployed to "
                               "recreate it")
                    out.append({"id": app["id"], "redeployed": True,
                                "reason": "no container — the app could not wake"})
                    continue
                labels = await _current_labels(app["coolify_uuid"])
                if not labels or is_enrolled(labels, app["id"]):
                    continue
                from . import routing  # lazy: routing imports this module
                await routing.apply_backend_labels(
                    app["id"], app["coolify_uuid"], sleeps=True,
                    fronted=routing.is_fronted(app))
                await c.execute(
                    "UPDATE apps SET sablier_enrolled = true WHERE id = $1",
                    app["id"])
                out.append({"id": app["id"], "re_enrolled": True})
            except Exception as e:
                out.append({"id": app["id"], "error": str(e)})
    return out


