"""Unauthenticated deploy trigger for CI.

POST /apps/<id>/refresh pulls the app's watched tag and redeploys it if the
image digest moved — a no-op otherwise. It is deliberately unauthenticated so
the CI workflow can call it with no repo secret. That is safe because it takes
no caller-supplied image: an unauthenticated caller can only make the app pull
its own newest image (which only whoever can push to the registry controls), so
the worst case is triggering a check. It is not exposed as an MCP tool — it is a
pipeline hook, not a model action.
"""
from fastapi import APIRouter, HTTPException

from ..db import pool
from .. import autoupdate

router = APIRouter(prefix="/apps", tags=["refresh"])


@router.post("/{app_id}/refresh", operation_id="apps_refresh",
             summary="Pull the app's watched tag and redeploy if the image changed")
async def refresh(app_id: str):
    async with pool().acquire() as c:
        app = await c.fetchrow(
            f"SELECT {autoupdate.APP_COLS} FROM apps WHERE id = $1", app_id)
        if not app:
            raise HTTPException(404, "no such app")
        return await autoupdate.check_and_update(c, app)
