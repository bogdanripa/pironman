"""Frontend deploys and routing configuration.

The upload endpoint is what an app's CI calls after building its static bundle.
It takes the app's scoped deploy key — the same PAAS_KEY the backend's /refresh
hook uses, which can only touch that one app. Both halves of a deploy are
authenticated with it, so an app's pipeline needs exactly one secret.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import require_key
from ..db import pool
from ..locks import app_lock
from ..config import app_url
from .. import cdn, frontends, routing

router = APIRouter(prefix="/apps", tags=["frontend"],
                   dependencies=[Depends(require_key)])


async def _sync_manifest(c, app_id: str) -> dict:
    row = await c.fetchrow(
        "SELECT image, has_frontend, redirects, spa FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app")
    has_backend = bool(row["image"])
    # Rewrite the whole manifest — redirects and the SPA flag included — since a
    # frontend deploy must not drop settings the static host is relying on.
    frontends.write_manifest(app_id, has_backend, list(row["redirects"] or []),
                             spa=bool(row["spa"]))
    # Make sure the static host actually has a route for this hostname. A no-op
    # once the route exists, so repeat deploys don't restart the shared host.
    try:
        routed = await routing.sync_frontend_routes(c)
    except Exception as e:  # routing must never fail an otherwise-good deploy
        routed = {"error": str(e)}
    return {"has_backend": has_backend, "routing": routed}


@router.put("/{app_id}/frontend", operation_id="apps_frontend_deploy",
            summary="Upload a static frontend bundle (zip) for an app",
            include_in_schema=True)
async def deploy_frontend(app_id: str, request: Request):
    """Deploy a frontend by uploading its built static files as a zip.

    Send the raw zip as the request body (`--data-binary @site.zip`). The zip
    should contain the *contents* of your build output directory — index.html at
    its root; a single wrapping directory (`dist/`) is unwrapped automatically.

    The bundle is unpacked and swapped in atomically, so the site is never served
    half-written and nothing restarts — a frontend deploy takes about a second.
    This is the call an app's CI makes after `npm run build`; it needs the app's
    scoped deploy key — the same one the backend's /refresh hook uses.
    """
    # Serialised: this can rewrite routing labels and redeploy, and an app's CI
    # ships its frontend and its backend at the same time.
    async with app_lock(app_id), pool().acquire() as c:
        if not await c.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
            raise HTTPException(404, "no such app")
        body = await request.body()
        if not body:
            raise HTTPException(400, "empty body — send the zip as the request body")
        try:
            res = frontends.deploy_bundle(app_id, body)
        except frontends.FrontendError as e:
            raise HTTPException(400, str(e))
        await c.execute("UPDATE apps SET has_frontend = true WHERE id = $1", app_id)
        cfg = await _sync_manifest(c, app_id)
    purged = await cdn.purge(app_id, res.pop("paths", []))
    return {"id": app_id, "deployed": True, "url": app_url(app_id),
            **res, **cfg, "cdn": purged}


class FrontendFiles(BaseModel):
    files: dict[str, str] = Field(
        description="The site's files as {path: text content}, e.g. "
                    "{'index.html': '<!doctype html>…', 'style.css': 'body{…}'}. "
                    "Paths are relative to the site root and must include an "
                    "index.html. Text only — for images or a compiled build, ship "
                    "a zip through the app's CI instead.")


@router.put("/{app_id}/frontend-files", operation_id="apps_frontend_write",
            summary="Publish a small static site from inline files")
async def write_frontend(app_id: str, body: FrontendFiles):
    """Publish a static site by writing its files directly — no build, no
    repository, no CI.

    For a landing page, a status page or a redirect stub this is the whole
    deploy: the files are written and served in about a second, and the app is
    live at its URL. For a real application (a compiled SPA, anything with
    images), have the app's CI upload a zip instead — see apps_deploy_workflow.

    Replaces the whole site: files not included here are removed. Include
    index.html; it is what "/" resolves to. Add a 404.html and it is served for
    paths the site does not have. If the site is a single-page app whose router
    owns those paths, set apps_update spa=true so they serve index.html instead.
    """
    async with app_lock(app_id), pool().acquire() as c:
        if not await c.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
            raise HTTPException(404, "no such app")
        try:
            res = frontends.deploy_files(app_id, body.files)
        except frontends.FrontendError as e:
            raise HTTPException(400, str(e))
        await c.execute("UPDATE apps SET has_frontend = true WHERE id = $1", app_id)
        cfg = await _sync_manifest(c, app_id)
    purged = await cdn.purge(app_id, res.pop("paths", []))
    return {"id": app_id, "deployed": True, "url": app_url(app_id),
            **res, **cfg, "cdn": purged}
