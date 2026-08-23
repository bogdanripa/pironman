"""Auto-update: pull an app's watched tag and redeploy when its image changes.

This removes the authenticated deploy call (and its repo secret) from CI — the
box watches the registry itself. Two entry points share one routine,
check_and_update:

  - an hourly sweep over every app that opts in (watch_tag set), and
  - an authenticated POST /apps/<id>/refresh the CI workflow curls right after
    it pushes a new image, for an immediate deploy instead of waiting the hour.

Digest resolution goes through the host Docker daemon (the mounted socket)
rather than talking to the registry directly, so it borrows the daemon's
existing pull credentials — nothing new to configure. A `docker pull` on an
unchanged tag is a cheap no-op, so both paths are idempotent: they redeploy only
when the tag's digest actually moved.
"""
import asyncio
import logging

from . import coolify, envs, events
from .config import GHCR_USER, GHCR_TOKEN, SABLIER_AUTO_ENROLL, app_fqdn
from .db import pool

_log = logging.getLogger("pironman.autoupdate")

_ghcr_logged_in = False

# Everything check_and_update needs to compare digests, redeploy, and enroll.
APP_COLS = ("id, coolify_uuid, image, watch_tag, deployed_digest, "
            "db_engine, db_user, db_password, db_name, "
            "sleep_when_idle, sablier_enrolled, internal")


def repo_of(image: str) -> str:
    """The image reference without its tag or digest, e.g.
    'ghcr.io/bogdanripa/notes:latest' -> 'ghcr.io/bogdanripa/notes'."""
    ref = image.split("@", 1)[0]
    slash, colon = ref.rfind("/"), ref.rfind(":")
    return ref[:colon] if colon > slash else ref


def tag_of(image: str, default: str = "latest") -> str:
    """The tag of an image reference, or `default` if it has none. A colon only
    counts as a tag separator when it comes after the last '/', so a registry
    port (host:5000/img) is not mistaken for a tag."""
    ref = image.split("@", 1)[0]
    slash, colon = ref.rfind("/"), ref.rfind(":")
    return ref[colon + 1:] if colon > slash else default


async def _docker(*args: str, timeout: int = 600) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "docker timed out"
    return proc.returncode, out.decode()


async def ensure_ghcr_login() -> None:
    """Log the container's docker CLI in to ghcr.io so digest-check pulls can
    reach private packages. The daemon's own creds are not used by `docker pull`
    (the CLI supplies auth), and this container has none by default. No-op when
    GHCR_TOKEN is unset (public images still work) or after the first success."""
    global _ghcr_logged_in
    if _ghcr_logged_in or not GHCR_TOKEN:
        return
    proc = await asyncio.create_subprocess_exec(
        "docker", "login", "ghcr.io", "-u", GHCR_USER or "x-access-token",
        "--password-stdin",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    await proc.communicate(GHCR_TOKEN.encode())
    _ghcr_logged_in = proc.returncode == 0


async def remote_digest(ref: str) -> str | None:
    """Pull the tag fresh and return the image digest (sha256:...), or None if
    the pull/inspect failed. The pull also warms the local image so the Coolify
    redeploy that follows reliably gets the new bits — which is why watching a
    moving tag like :latest works here."""
    await ensure_ghcr_login()
    rc, _ = await _docker("pull", ref)
    if rc != 0:
        return None
    rc, out = await _docker("inspect", "--format", "{{index .RepoDigests 0}}", ref)
    if rc != 0 or "@" not in out:
        return None
    return out.strip().split("@", 1)[1]


async def _container_name(uuid: str) -> str | None:
    """The most-recent container for a Coolify app (its name contains the app
    uuid). Includes stopped/crashed ones so a failed deploy is still inspectable."""
    _, out = await _docker("ps", "-a", "--filter", f"name={uuid}",
                           "--format", "{{.Names}}", timeout=30)
    names = [n for n in out.splitlines() if n.strip()]
    return names[0] if names else None


async def app_logs(uuid: str, tail: int = 200) -> dict:
    """Container status/health plus the last `tail` log lines, straight from the
    Docker daemon — the thing to reach for when a deploy 'succeeds' but the app
    serves 502."""
    name = await _container_name(uuid)
    if not name:
        return {"container": None,
                "status": "no container — the deploy failed or was rolled back",
                "logs": ""}
    _, status = await _docker(
        "inspect", "--format",
        "{{.State.Status}}{{if .State.Health}} ({{.State.Health.Status}}){{end}}",
        name, timeout=30)
    _, logs = await _docker("logs", "--tail", str(tail), name, timeout=60)
    return {"container": name, "status": status.strip(), "logs": logs}


async def _container_started_at(uuid: str) -> str | None:
    """RFC3339 start time of the app's current container, or None if there is
    none. Used to tell a container that a deploy actually replaced from one that
    merely survived it."""
    name = await _container_name(uuid)
    if not name:
        return None
    rc, out = await _docker("inspect", "--format", "{{.State.StartedAt}}", name,
                            timeout=30)
    return out.strip() if rc == 0 else None


async def verify_deploy(uuid: str, before: str | None, timeout: int = 150) -> dict:
    """Wait for a deploy to actually take, and say so honestly if it did not.

    Coolify's deploy call is asynchronous and its rollback is silent: if the new
    container fails to start or fails its healthcheck, Coolify removes it and the
    PREVIOUS container keeps serving. Everything then looks healthy — the deploy
    call returned 2xx, a container exists, and it reports running — while the code
    that was deployed is nowhere. That failure cost a long debugging session, so
    it is worth the wait to catch it.

    A deploy is verified when the app's container has a start time later than the
    one observed before the deploy (i.e. it was genuinely replaced) and is running
    without an unhealthy healthcheck.
    """
    deadline = timeout
    last = {"replaced": False, "status": None}
    while deadline > 0:
        name = await _container_name(uuid)
        if name:
            _, started = await _docker(
                "inspect", "--format", "{{.State.StartedAt}}", name, timeout=30)
            _, status = await _docker(
                "inspect", "--format",
                "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}"
                "{{else}}none{{end}}", name, timeout=30)
            started, status = started.strip(), status.strip()
            state, health = (status.split("|", 1) + ["none"])[:2]
            replaced = bool(started) and (before is None or started > before)
            last = {"replaced": replaced, "status": status, "container": name}
            if replaced and state == "running" and health in ("healthy", "none"):
                return {"verified": True, "container": name}
        await asyncio.sleep(5)
        deadline -= 5

    return {
        "verified": False,
        **last,
        "reason": ("the new container never became healthy, so Coolify rolled the "
                   "deploy back and the previous container is still serving — the "
                   "code you deployed is not running. apps_logs shows why it "
                   "failed to start."
                   if not last["replaced"] else
                   "the container was replaced but did not become healthy in time"),
    }


class NoSuchApp(LookupError):
    pass


async def apply_image(app_id: str, image: str) -> dict:
    """Point an app at `image` and redeploy, creating its container first if it
    does not have one yet.

    This is how an app gets a backend at all. Registration (apps_create) writes
    only an id — it deliberately does not ask what the app will run, because only
    the app's pipeline knows that — so the first CI deploy is what turns the
    registration into a container. Also repoints auto-update at the new tag and
    clears the recorded digest, so the app tracks the right image from here on.
    """
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "SELECT coolify_uuid, db_engine, db_user, db_password, db_name, "
            "watch_tag, health_path, internal FROM apps WHERE id = $1", app_id)
        if not row:
            raise NoSuchApp(app_id)

        # No Coolify application yet: create one (and its healthcheck) before
        # pointing it anywhere. sleep_when_idle turns on here rather than at
        # registration — until now there was no container for it to describe.
        gained_backend = not row["coolify_uuid"]
        if gained_backend:
            # An internal app is created with NO domain. That single
            # difference is what makes a database unreachable from outside:
            # Coolify derives its Traefik labels from the domain, so without
            # one there is no router, no certificate and no public hostname —
            # only the container alias on the shared `coolify` network.
            uuid = await coolify.create_app(
                image, "" if row["internal"] else app_fqdn(app_id), app_id=app_id)
            try:
                await coolify.set_healthcheck(uuid, row["health_path"] or "/")
            except Exception:
                pass  # a missing healthcheck must not strand a created app
            if row["internal"]:
                # A readable, stable address for the apps that will call it:
                # http://rag:80 rather than http://<uuid>:80. Logged rather than
                # raised -- the app is already created, and envs.internal_urls
                # falls back to the uuid, which always resolves.
                try:
                    if await coolify.set_network_alias(uuid, app_id):
                        await c.execute(
                            "UPDATE apps SET internal_alias = $1 WHERE id = $2",
                            app_id, app_id)
                    else:
                        _log.warning(
                            "network alias %r did not take for %s; callers will "
                            "use the uuid address instead", app_id, app_id)
                except Exception as exc:
                    _log.warning("could not set network alias for %s: %s",
                                 app_id, exc)
            # Internal apps must not sleep. Scale-to-zero is driven by the
            # Sablier middleware on a Traefik router, and an internal app has
            # no router — so nothing would ever wake it and the first caller
            # would simply get a refused connection. Databases stay up for
            # exactly the same reason.
            await c.execute(
                "UPDATE apps SET coolify_uuid = $1, sleep_when_idle = $2 "
                "WHERE id = $3", uuid, not row["internal"], app_id)
            row = await c.fetchrow(
                "SELECT coolify_uuid, db_engine, db_user, db_password, db_name, "
                "watch_tag, health_path FROM apps WHERE id = $1", app_id)

        before = await _container_started_at(row["coolify_uuid"])
        await coolify.set_image(row["coolify_uuid"], image)
        await envs.sync_env(c, row["coolify_uuid"], app_id, row["db_engine"],
                            row["db_user"], row["db_password"], row["db_name"])
        await coolify.deploy(row["coolify_uuid"], app_id=app_id,
                             reason=f"code update: image set to {image}")
        # Keep auto-update following the new tag (only if it was already on) — and
        # turn it on for a backend that has just been added.
        watch = (tag_of(image)
                 if (gained_backend or row["watch_tag"] is not None) else None)
        await c.execute(
            "UPDATE apps SET image = $1, watch_tag = $2, deployed_digest = NULL "
            "WHERE id = $3", image, watch, app_id)

    return {"id": app_id, "image": image, "watch_tag": watch,
            "created_container": gained_backend,
            "coolify_uuid": row["coolify_uuid"], "started_before": before}


async def settle(app_id: str, uuid: str, before: str | None) -> dict:
    """Confirm a deploy landed, then finish the app's setup: record the running
    digest, enrol it in scale-to-zero and put the static host in front of it.

    Enrolment has to come after the container exists — its labels are read off the
    running container — which is why this is a step of its own rather than part of
    apply_image."""
    result = await verify_deploy(uuid, before)
    if not result.get("verified"):
        return result
    async with pool().acquire() as c:
        app = await c.fetchrow(f"SELECT {APP_COLS} FROM apps WHERE id = $1", app_id)
        if app and app["image"]:
            digest = await remote_digest(app["image"])
            if digest:
                await c.execute(
                    "UPDATE apps SET deployed_digest = $1 WHERE id = $2",
                    digest, app_id)
            # Skipped when enrollment just ran: that write queues its own
            # redeploy, which would start the container straight back up and
            # leave the app awake anyway.
            if not await _maybe_enroll_sablier(c, app):
                result["sleep"] = await sleep_after_deploy(app)
    return result


async def check_and_update(conn, app, verify: bool = False) -> dict:
    """Pull the app's watched tag; if its digest differs from what is running,
    redeploy (re-injecting env like a normal deploy) and record the new digest.
    A no-op when nothing changed. `app` is a row selected with APP_COLS."""
    watch = app["watch_tag"] or "latest"
    ref = f'{repo_of(app["image"])}:{watch}'

    digest = await remote_digest(ref)
    if digest is None:
        return {"id": app["id"], "updated": False, "error": f"could not pull {ref}"}
    if digest == app["deployed_digest"]:
        return {"id": app["id"], "updated": False, "image": ref}

    before = await _container_started_at(app["coolify_uuid"])
    await coolify.set_image(app["coolify_uuid"], ref)
    await envs.sync_env(conn, app["coolify_uuid"], app["id"], app["db_engine"],
                        app["db_user"], app["db_password"], app["db_name"])
    await coolify.deploy(
        app["coolify_uuid"], app_id=app["id"],
        reason=f"auto-update: the digest behind {ref} moved",
        )
    await conn.execute(
        "UPDATE apps SET image = $1, deployed_digest = $2 WHERE id = $3",
        ref, digest, app["id"])
    enrolled = await _maybe_enroll_sablier(conn, app)
    result = {"id": app["id"], "updated": True, "image": ref, "digest": digest}
    if verify:
        result.update(await verify_deploy(app["coolify_uuid"], before))
        # Only once the deploy is confirmed good, and not when enrollment just
        # queued a redeploy of its own. Without verify there is nothing to
        # confirm against, so the app is left running rather than slept blind.
        if result.get("verified") and not enrolled:
            result["sleep"] = await sleep_after_deploy(app)
    return result


async def _maybe_enroll_sablier(conn, app) -> bool:
    """Auto-enroll an app in Sablier scale-to-zero after a deploy, once, if the
    feature is on and the app opts in. Gated by SABLIER_AUTO_ENROLL (off by
    default) so a wrong SABLIER_URL can't break routing platform-wide before it's
    verified on one app. Best-effort: a failure just leaves it for next time.

    Returns whether enrollment actually ran, because writing those labels queues
    a redeploy of its own — so the caller must not also try to put the app to
    sleep in the same pass."""
    if not SABLIER_AUTO_ENROLL or app["sablier_enrolled"] or not app["sleep_when_idle"]:
        return False
    from . import routing, sablier  # lazy: both import this module
    if sablier.excluded(app["id"]):
        return False
    try:
        # The static host's router comes FIRST. From the moment this app is
        # enrolled it can be stopped, and Traefik drops a stopped container's
        # router — so without something awake holding its hostname there would be
        # nothing left to wake it. scope_backends=False because the enrollment
        # write below covers this app's own labels in one pass.
        await routing.sync_frontend_routes(conn, scope_backends=False)
        await routing.apply_backend_labels(
            app["id"], app["coolify_uuid"], sleeps=True, fronted=True)
        await conn.execute(
            "UPDATE apps SET sablier_enrolled = true WHERE id = $1", app["id"])
        return True
    except Exception:
        return False  # container may not be up yet; a later sweep retries


async def sleep_after_deploy(app) -> dict | None:
    """Put a freshly deployed scale-to-zero app straight back to sleep.

    Sablier only stops instances it holds a session for, and sessions are created
    by requests arriving through its middleware — never by a deploy. So an app
    that has been deployed but not yet called has nothing to expire and stays up
    for ever, which makes sleep_when_idle quietly untrue until the first request
    happens to arrive. Stopping it here makes the flag mean what it says from the
    moment the deploy lands; the next request wakes it exactly as it would after
    any idle period.

    Only ever called AFTER verify_deploy has passed. Verification is what
    separates a good deploy from Coolify's silent rollback, and it can only tell
    them apart while the container is running — stopping first would erase the
    difference between "deployed and asleep" and "deploy failed, nothing there".

    Returns None when the app is not a candidate, so callers can tell "did not
    apply" from "tried and failed"."""
    from . import sablier  # lazy: imports this module
    if not app["sleep_when_idle"] or sablier.excluded(app["id"]):
        return None
    # Enrollment is what makes an app wakeable: the Sablier middleware on its
    # router is the only thing that will ever start it again. Stopping an app
    # that opted into sleeping but was never enrolled — SABLIER_AUTO_ENROLL is
    # off by default, so that combination is entirely normal — would strand it
    # with nothing able to bring it back. Sleep only what can wake.
    if not app["sablier_enrolled"]:
        return None
    name = await _container_name(app["coolify_uuid"])
    if not name:
        return None
    rc, out = await _docker("stop", name, timeout=90)
    # The other half of the recycle record. A deploy leaves a new container to
    # ask questions of; a stop leaves nothing running at all, and its router
    # disappears with it — so "why is this app not answering" has no local
    # evidence unless it was written down before the container went away.
    await events.record(
        app["id"], "stop" if rc == 0 else "stop_failed",
        "put to sleep after a deploy: sleep_when_idle is on and it is enrolled",
        container=name, **({} if rc == 0 else {"error": out.strip()[:200]}))
    return {"slept": rc == 0, "container": name,
            **({} if rc == 0 else {"error": out.strip()[:200]})}


async def check_all() -> list[dict]:
    """Check every opted-in app once. One app's failure never aborts the sweep.

    Deploys made here are verified, and a rollback is reported: nobody is watching
    a background sweep, so a silently reverted deploy would otherwise sit
    undiscovered until someone noticed a change had gone missing.
    """
    from . import notify

    async with pool().acquire() as c:
        apps = await c.fetch(
            f"SELECT {APP_COLS} FROM apps WHERE watch_tag IS NOT NULL ORDER BY id")
        results = []
        for app in apps:
            try:
                res = await check_and_update(c, app, verify=True)
                results.append(res)
                if res.get("updated") and res.get("verified") is False:
                    await notify.send(
                        f"⚠️ <b>{app['id']}</b> deploy rolled back\n"
                        f"{res.get('reason', '')}\n"
                        f"The previous version is still serving.")
            except Exception as e:
                results.append({"id": app["id"], "updated": False, "error": str(e)})
        try:
            await warn_missing_healthchecks(c)
        except Exception:
            _log.exception("healthcheck audit failed")  # never abort the sweep
    return results


async def warn_missing_healthchecks(conn) -> list[str]:
    """Name any app container running without a HEALTHCHECK of any kind.

    `set_healthcheck` deliberately leaves Coolify's own check off so the image's
    governs — the image is the only place Docker's `--start-interval` can be
    set, and that flag is the difference between a 5.5s cold wake and a 0.76s
    one. Coolify still verifies a deploy in that arrangement, because it falls
    back to reading `.State.Health.Status` when it finds a check in the image.

    But only then. With no check anywhere, Coolify's own condition
    (`isHealthcheckDisabled() && custom_healthcheck_found === false`) sets
    `newVersionIsHealthy = true` and returns — so the deploy is reported
    successful without being verified, and Sablier has nothing to distinguish a
    started container from a ready one. Both losses are silent, and a deploy
    that was never checked reads exactly like one that passed, which is the
    failure this platform is built against (§12). Hence the warning: it is the
    only thing standing between that state and nobody noticing.
    """
    rows = await conn.fetch(
        "SELECT id, coolify_uuid FROM apps WHERE coolify_uuid IS NOT NULL "
        "ORDER BY id")
    missing = []
    for r in rows:
        name = await _container_name(r["coolify_uuid"])
        if not name:
            continue  # not running; nothing to inspect, and not our business here
        rc, out = await _docker(
            "inspect", "--format",
            "{{if .Config.Healthcheck}}yes{{else}}no{{end}}", name, timeout=30)
        if rc == 0 and out.strip() == "no":
            missing.append(r["id"])
    if missing:
        _log.warning(
            "no HEALTHCHECK on %s — Coolify is NOT verifying their deploys and "
            "Sablier cannot tell a started container from a ready one. Add a "
            "HEALTHCHECK to the image; apps_deploy_workflow prints the line, "
            "and it needs --start-interval=250ms to wake in under a second.",
            ", ".join(missing))
    return missing
