"""Authenticated deploy hook for CI.

POST /apps/<id>/refresh is what an app's CI workflow calls right after pushing a
new image, so the deploy lands immediately instead of waiting for the hourly
sweep. It does two jobs, decided by whether the app already has a backend:

  - **First deploy.** Registration (apps_create) writes only an id — it does not
    ask what the app will run, because only the app's pipeline knows. So the
    first call must say which image it built, and that is what creates the
    Coolify application, its healthcheck and its container. Without this an app
    could be registered and then never deploy, which is exactly what happened.
  - **Every deploy after.** With or without an image in the body, it pulls the
    watched tag and redeploys if the digest moved — a no-op otherwise.

**It answers 202 and does the work off-request.** It used to hold the connection
open until `verify_deploy` had finished, and that wait is bounded at 150s while
Cloudflare — which fronts this API — cuts a request at ~100s. So the longest and
most interesting deploys were exactly the ones CI could not get an answer about:
measured 2026-08-23, two `POST /apps/gepetel/refresh` calls ran 125.0s and 125.0s
at the edge and both ended `499`, the client gone.

Worth being precise about what those two were, because the obvious reading is
wrong and the wrong reading argues for a worse design. They were not slow
successes. Coolify had **failed and rolled both back inside 20s** (deployment
queue rows 390 and 393: healthcheck unhealthy, `wget: not found`, the app
crashing on a missing key). `verify_deploy` cannot see Coolify's verdict, so it
polls the container to its own deadline — and the 524 arrived first, hiding the
502 this hook was about to return correctly. The timeout is therefore NOT too
short and must not be lengthened: Coolify decides in 8-49s on this box, so a
longer budget would only delay honest failures. What was wrong is that a request
was holding the wait at all.

Which is why returning early had to come with somewhere to look. Coolify rolls a
failed deploy back **silently**, leaving the previous container serving while
every other signal says success (§12) — so an app that has deployed before will
answer its healthcheck perfectly well with the old code in place, and a CI job
that only pings the app goes green on precisely the failure above. The
verification still runs; its result is written to the `deploys` table and read
back by **GET /apps/<id>/refresh**, which is what CI must gate on. A failure also
raises a Telegram alert, the same way the hourly sweep's does — nobody is
watching a background deploy either.

Both verbs require the app's **scoped deploy key** (the PAAS_KEY repository
secret), the same key the frontend upload uses. That key can only touch this one
app, which is what makes accepting an image from the caller reasonable: the app's
own pipeline saying "this is what I built" is the direction information is
supposed to flow here. An unscoped caller cannot reach this at all.

Not exposed as MCP tools — this is a pipeline hook, not a model action. Deploys
go through CI, and giving the model a manual-deploy button invites skipping it.
`platform_events` already shows a model the same deploys from the other side.
"""
import asyncio
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ..auth import require_key
from ..db import pool
from ..locks import app_lock
from .. import autoupdate, deploys, notify

_log = logging.getLogger("pironman.refresh")

router = APIRouter(prefix="/apps", tags=["refresh"],
                   dependencies=[Depends(require_key)])

# Strong references to the deploys currently running. asyncio keeps only a weak
# reference to a running task, so a task nobody holds can be garbage-collected
# mid-await — the deploy would simply stop happening, at no particular point,
# with nothing logged.
_running: set[asyncio.Task] = set()


class RefreshBody(BaseModel):
    image: str | None = Field(
        default=None,
        description="The image this build pushed, e.g. "
                    "'ghcr.io/owner/repo:latest'. Required on an app's FIRST "
                    "backend deploy — it is what creates the container. Optional "
                    "afterwards: the box already watches the tag.")


async def _deploy(app_id: str, image: str | None) -> dict:
    """The deploy itself, exactly as it ran when it was inline. Serialised
    against this app's other deploy paths: an app that ships a frontend and a
    backend runs both CI jobs at once, and a frontend upload can redeploy the app
    too (routing labels). Overlapping Coolify deploys of one app do not queue —
    they fight.

    The lock is taken here rather than around the whole request so a second
    /refresh still answers 202 straight away and queues behind the first, instead
    of holding a connection open waiting for a turn."""
    async with app_lock(app_id):
        async with pool().acquire() as c:
            app = await c.fetchrow(
                f"SELECT {autoupdate.APP_COLS} FROM apps WHERE id = $1", app_id)
        # Re-checked under the lock: the request validated this before answering
        # 202, and an app can be deleted in between.
        if not app:
            return {"error": "no such app — it was deleted after the deploy was "
                             "queued"}

        if not app["image"] or not app["coolify_uuid"]:
            res = await autoupdate.apply_image(app_id, image)
            check = await autoupdate.settle(app_id, res["coolify_uuid"],
                                            res["started_before"])
            return {"id": app_id, "image": res["image"],
                    "watch_tag": res["watch_tag"], "created_container": True,
                    **check}

        # verify=True: Coolify rolls a failed deploy back silently, so without
        # this the hook records success while the previous container keeps
        # serving — CI goes green and the change is simply missing.
        async with pool().acquire() as c:
            return await autoupdate.check_and_update(c, app, verify=True)


async def _run(deploy_id: str, app_id: str, image: str | None) -> None:
    """Run a queued deploy and write down how it ended. Never raises: this is a
    bare task with no caller left to catch anything, so an escaping exception
    would be a deploy that vanishes with only a traceback in a container log that
    the next deploy destroys."""
    try:
        result = await _deploy(app_id, image)
    except Exception as exc:
        _log.exception("deploy %s of %s failed", deploy_id, app_id)
        result = {"error": repr(exc)}

    # Three distinct outcomes collapse to one boolean here, so be explicit about
    # which is which. `verified is False` is a rollback — the deploy ran and the
    # code is not live. `error` is a failure to deploy at all. Everything else is
    # a pass, INCLUDING `updated: False`, which means the digest had not moved
    # and there was correctly nothing to do.
    failed = result.get("verified") is False or bool(result.get("error"))
    await deploys.finish(deploy_id, not failed, result=result)

    if failed:
        await notify.send(
            f"⚠️ <b>{app_id}</b> deploy failed\n"
            f"{result.get('reason') or result.get('error') or ''}\n"
            f"deploy <code>{deploy_id}</code> — apps_logs {app_id} shows why.")


@router.post("/{app_id}/refresh", operation_id="apps_refresh", status_code=202,
             summary="Queue a deploy of the app's image — creating its container "
                     "on the first call")
async def refresh(app_id: str, response: Response, body: RefreshBody | None = None):
    """Queue the deploy and answer 202 immediately; poll GET /apps/<id>/refresh
    for the outcome.

    What is still decided synchronously is only what can be decided without
    deploying: that the app exists, and that a first-time deploy said what image
    to run. Those are caller mistakes, and a caller mistake should come back as
    an error on the call that made it rather than as a status to go and look up.
    """
    image = body.image if body else None

    async with pool().acquire() as c:
        app = await c.fetchrow(
            "SELECT image, coolify_uuid FROM apps WHERE id = $1", app_id)
    if not app:
        raise HTTPException(404, "no such app")
    if not (app["image"] and app["coolify_uuid"]) and not image:
        raise HTTPException(400, {
            "error": "this app has no backend yet",
            "fix": "send the image this build pushed, e.g. "
                   '{"image": "ghcr.io/owner/repo:latest"} — the first '
                   "call is what creates the container",
            "note": "if the app is frontend-only, it deploys by uploading "
                    "its bundle to PUT /apps/<id>/frontend instead",
        })

    deploy_id = secrets.token_urlsafe(9)
    # Written before the 202 so the id the caller is handed already resolves the
    # first time it polls. A caller that gets an id and then a 404 for it cannot
    # tell "too early" from "wrong id", and would have to guess.
    await deploys.start(deploy_id, app_id, image=image)

    task = asyncio.create_task(_run(deploy_id, app_id, image))
    _running.add(task)
    task.add_done_callback(_running.discard)

    response.headers["Location"] = f"/apps/{app_id}/refresh?deploy={deploy_id}"
    return {
        "queued": True,
        "id": app_id,
        "deploy": deploy_id,
        "status_url": f"/apps/{app_id}/refresh?deploy={deploy_id}",
        "note": "the deploy runs on the box and is verified there. This 202 says "
                "it started, NOT that it worked — poll status_url until `state` "
                "is 'succeeded' or 'failed'. A 'failed' state means Coolify "
                "rolled the deploy back and the previous container is still "
                "serving, which the app's own health endpoint cannot tell you.",
    }


@router.get("/{app_id}/refresh", operation_id="apps_refresh_status",
            summary="How a queued deploy ended")
async def refresh_status(app_id: str, deploy: str | None = None):
    """The outcome of a deploy queued by POST /apps/<id>/refresh — the one named
    by `deploy`, or the app's most recent if that is omitted.

    `state` is the field to branch on:

      - `succeeded` — the container was genuinely replaced and came up healthy.
      - `failed` — it did not, and Coolify rolled it back, so the PREVIOUS
        container is still serving. The app answers healthily and the code that
        was deployed is not running; this is the only place that difference is
        visible.
      - `running` — still going.
      - `unknown` — no record, or the control plane was recycled mid-deploy.
        Not a pass and not a fail; go and look at the app.
    """
    state = await deploys.get(app_id, deploy)
    if state is None:
        return {"app": app_id, "deploy": deploy, "state": "unknown",
                "note": "no deploy on record" + (
                    " with that id" if deploy else
                    " for this app — it has never deployed through /refresh")}
    return state
