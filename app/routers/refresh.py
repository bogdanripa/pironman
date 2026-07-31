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

It requires the app's **scoped deploy key** (the PAAS_KEY repository secret), the
same key the frontend upload uses. That key can only touch this one app, which is
what makes accepting an image from the caller reasonable: the app's own pipeline
saying "this is what I built" is the direction information is supposed to flow
here. An unscoped caller cannot reach this at all.

Not exposed as an MCP tool — it is a pipeline hook, not a model action. Deploys
go through CI, and giving the model a manual-deploy button invites skipping it.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_key
from ..db import pool
from ..locks import app_lock
from .. import autoupdate

router = APIRouter(prefix="/apps", tags=["refresh"],
                   dependencies=[Depends(require_key)])


class RefreshBody(BaseModel):
    image: str | None = Field(
        default=None,
        description="The image this build pushed, e.g. "
                    "'ghcr.io/owner/repo:latest'. Required on an app's FIRST "
                    "backend deploy — it is what creates the container. Optional "
                    "afterwards: the box already watches the tag.")


@router.post("/{app_id}/refresh", operation_id="apps_refresh",
             summary="Deploy the app's image — creating its container on the first call")
async def refresh(app_id: str, body: RefreshBody | None = None):
    image = body.image if body else None

    # Serialised against this app's other deploy paths: an app that ships a
    # frontend and a backend runs both CI jobs at once, and a frontend upload can
    # redeploy the app too (routing labels). Overlapping Coolify deploys of one
    # app do not queue — they fight.
    async with app_lock(app_id):
        async with pool().acquire() as c:
            app = await c.fetchrow(
                f"SELECT {autoupdate.APP_COLS} FROM apps WHERE id = $1", app_id)
        if not app:
            raise HTTPException(404, "no such app")

        if not app["image"] or not app["coolify_uuid"]:
            if not image:
                raise HTTPException(400, {
                    "error": "this app has no backend yet",
                    "fix": "send the image this build pushed, e.g. "
                           '{"image": "ghcr.io/owner/repo:latest"} — the first '
                           "call is what creates the container",
                    "note": "if the app is frontend-only, it deploys by uploading "
                            "its bundle to PUT /apps/<id>/frontend instead",
                })
            res = await autoupdate.apply_image(app_id, image)
            check = await autoupdate.settle(app_id, res["coolify_uuid"],
                                            res["started_before"])
            result = {"id": app_id, "image": res["image"],
                      "watch_tag": res["watch_tag"], "created_container": True,
                      **check}
        else:
            # verify=True: Coolify rolls a failed deploy back silently, so without
            # this the hook returns 2xx while the previous container keeps serving —
            # CI goes green and the change is simply missing.
            async with pool().acquire() as c:
                result = await autoupdate.check_and_update(c, app, verify=True)

    if result.get("verified") is False:
        raise HTTPException(502, {
            "deployed": False,
            "app": app_id,
            "reason": result.get("reason"),
            "hint": f"apps_logs {app_id} shows why the container failed to start",
        })
    return result
