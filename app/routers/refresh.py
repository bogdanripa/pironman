"""Authenticated deploy trigger for CI.

POST /apps/<id>/refresh pulls the app's watched tag and redeploys it if the image
digest moved — a no-op otherwise. It is what an app's CI workflow calls right
after pushing a new image, so the deploy lands immediately instead of waiting for
the hourly sweep.

It requires the app's **scoped deploy key** (the PAAS_KEY repository secret), the
same key the frontend upload uses. This endpoint was originally unauthenticated,
on the reasoning that it takes no caller-supplied image and so a caller could at
most trigger a digest check. That was true, but it stopped being a good trade
once the platform could install the secret itself (apps_create returns the key,
github_secret_set writes it to the repo): the only thing being bought was "no
human step", which is now free, while the cost was an endpoint anyone could hit
to make the box pull images. Authenticating both halves is simpler to explain and
leaves nothing open.

Not exposed as an MCP tool — it is a pipeline hook, not a model action. Deploys
go through CI, and giving the model a manual-deploy button invites skipping it.
"""
from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_key
from ..db import pool
from ..locks import app_lock
from .. import autoupdate

router = APIRouter(prefix="/apps", tags=["refresh"],
                   dependencies=[Depends(require_key)])


@router.post("/{app_id}/refresh", operation_id="apps_refresh",
             summary="Pull the app's watched tag and redeploy if the image changed")
async def refresh(app_id: str):
    # Serialised against this app's other deploy paths: an app that ships a
    # frontend and a backend runs both CI jobs at once, and a frontend upload can
    # redeploy the app too (routing labels). Overlapping Coolify deploys of one
    # app do not queue — they fight.
    async with app_lock(app_id), pool().acquire() as c:
        app = await c.fetchrow(
            f"SELECT {autoupdate.APP_COLS} FROM apps WHERE id = $1", app_id)
        if not app:
            raise HTTPException(404, "no such app")
        if not app["image"] or not app["coolify_uuid"]:
            # Frontend-only: nothing to pull. Its CI uploads a bundle instead
            # (PUT /apps/<id>/frontend), so this hook has nothing to do.
            raise HTTPException(
                400, "frontend-only app — there is no image to refresh; deploy it "
                     "by uploading its bundle to PUT /apps/<id>/frontend")
        # verify=True: Coolify rolls a failed deploy back silently, so without
        # this the hook returns 2xx while the previous container keeps serving —
        # CI goes green and the change is simply missing.
        result = await autoupdate.check_and_update(c, app, verify=True)

    if result.get("updated") and result.get("verified") is False:
        raise HTTPException(502, {
            "deployed": False,
            "app": app_id,
            "reason": result.get("reason"),
            "hint": f"apps_logs {app_id} shows why the container failed to start",
        })
    return result
