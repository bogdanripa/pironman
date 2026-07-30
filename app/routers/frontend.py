"""Frontend deploys and routing configuration.

The upload endpoint is what an app's CI calls after building its static bundle.
Unlike the backend's /refresh hook it CANNOT be unauthenticated: /refresh takes no
caller content (it only makes the box pull an image the registry already
controls), whereas a zip is caller-supplied content that gets served on the app's
own domain. So it takes the app's scoped deploy key — the same PAAS_KEY an app
already mints, which can only touch that one app.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import require_key
from ..db import pool
from ..config import app_url
from .. import frontends

router = APIRouter(prefix="/apps", tags=["frontend"],
                   dependencies=[Depends(require_key)])


class BackendRoutes(BaseModel):
    routes: list[str] = Field(
        default_factory=list,
        description="Path prefixes the BACKEND owns outright, e.g. "
                    "['/auth', '/oauth/callback']. Usually empty: requests are "
                    "resolved automatically (a real file is served, a browser "
                    "navigation gets index.html, anything else goes to the "
                    "backend). Declare a prefix only where that guess is wrong — "
                    "typically OAuth callbacks, file downloads, or server-rendered "
                    "pages, which look like navigations but must hit the backend. "
                    "Use ['/'] to give the backend the whole host.")


async def _sync_manifest(c, app_id: str) -> dict:
    row = await c.fetchrow(
        "SELECT image, has_frontend, backend_routes FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app")
    has_backend = bool(row["image"])
    frontends.write_manifest(app_id, has_backend, list(row["backend_routes"] or []))
    return {"has_backend": has_backend,
            "backend_routes": list(row["backend_routes"] or [])}


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
    scoped deploy key, unlike the backend's unauthenticated /refresh hook.
    """
    async with pool().acquire() as c:
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
    return {"id": app_id, "deployed": True, "url": app_url(app_id), **res, **cfg}


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
    index.html; it is what a browser navigation resolves to, so SPA-style deep
    links work automatically.
    """
    async with pool().acquire() as c:
        if not await c.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
            raise HTTPException(404, "no such app")
        try:
            res = frontends.deploy_files(app_id, body.files)
        except frontends.FrontendError as e:
            raise HTTPException(400, str(e))
        await c.execute("UPDATE apps SET has_frontend = true WHERE id = $1", app_id)
        cfg = await _sync_manifest(c, app_id)
    return {"id": app_id, "deployed": True, "url": app_url(app_id), **res, **cfg}


@router.put("/{app_id}/backend-routes", operation_id="apps_backend_routes",
            summary="Declare which paths the backend owns (escape hatch)")
async def set_backend_routes(app_id: str, body: BackendRoutes):
    """Override request resolution for an app that has both a frontend and a
    backend.

    By default nothing needs declaring: a request for a real file gets the file,
    a browser navigation gets index.html (so SPA deep links work), and everything
    else goes to the backend with its own status codes intact. Declare prefixes
    here only for the cases that guess wrong — an OAuth callback, a download link,
    or a server-rendered page, which arrive as navigations but belong to the
    backend. `/api` is a good convention for new apps but is not required.
    """
    routes = [r if r.startswith("/") else "/" + r for r in body.routes]
    async with pool().acquire() as c:
        if not await c.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
            raise HTTPException(404, "no such app")
        await c.execute("UPDATE apps SET backend_routes = $1 WHERE id = $2",
                        routes, app_id)
        cfg = await _sync_manifest(c, app_id)
    return {"id": app_id, **cfg}
