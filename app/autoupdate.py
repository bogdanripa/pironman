"""Auto-update: pull an app's watched tag and redeploy when its image changes.

This removes the authenticated deploy call (and its repo secret) from CI — the
box watches the registry itself. Two entry points share one routine,
check_and_update:

  - an hourly sweep over every app that opts in (watch_tag set), and
  - an unauthenticated POST /apps/<id>/refresh the CI workflow curls right after
    it pushes a new image, for an immediate deploy instead of waiting the hour.

Digest resolution goes through the host Docker daemon (the mounted socket)
rather than talking to the registry directly, so it borrows the daemon's
existing pull credentials — nothing new to configure. A `docker pull` on an
unchanged tag is a cheap no-op, so both paths are idempotent: they redeploy only
when the tag's digest actually moved, which is what makes the unauthenticated
/refresh safe to expose (a caller cannot inject an image, only trigger a check).
"""
import asyncio

from . import coolify, envs
from .config import GHCR_USER, GHCR_TOKEN
from .db import pool

_ghcr_logged_in = False

# Everything check_and_update needs to compare digests and redeploy.
APP_COLS = ("id, coolify_uuid, image, watch_tag, deployed_digest, "
            "db_engine, db_user, db_password, db_name")


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


async def check_and_update(conn, app) -> dict:
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

    await coolify.set_image(app["coolify_uuid"], ref)
    await envs.sync_env(conn, app["coolify_uuid"], app["id"], app["db_engine"],
                        app["db_user"], app["db_password"], app["db_name"])
    await coolify.deploy(app["coolify_uuid"])
    await conn.execute(
        "UPDATE apps SET image = $1, deployed_digest = $2 WHERE id = $3",
        ref, digest, app["id"])
    return {"id": app["id"], "updated": True, "image": ref, "digest": digest}


async def check_all() -> list[dict]:
    """Check every opted-in app once. One app's failure never aborts the sweep."""
    async with pool().acquire() as c:
        apps = await c.fetch(
            f"SELECT {APP_COLS} FROM apps WHERE watch_tag IS NOT NULL ORDER BY id")
        results = []
        for app in apps:
            try:
                results.append(await check_and_update(c, app))
            except Exception as e:
                results.append({"id": app["id"], "updated": False, "error": str(e)})
    return results
