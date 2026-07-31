"""Redirect rules — CRUD over an app's ordered redirect list.

Rules live on the app and are applied by the shared static host before anything
else it does, so they work whether the app is a static site, a backend, or both.
An app with redirects but no frontend is routed through the static host too,
which is why setting a rule can move where its hostname points.
"""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_key
from ..db import pool
from .. import frontends, routing

router = APIRouter(prefix="/apps", tags=["redirects"],
                   dependencies=[Depends(require_key)])

STATUSES = (301, 302, 307, 308)
_PLACEHOLDER = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")


class Redirect(BaseModel):
    from_: str = Field(
        alias="from",
        description="Path to match, e.g. '/old-page'. Supports '*' for the rest "
                    "of the path (available in `to` as ':splat') and ':name' for "
                    "a single segment (available as ':name'). With neither, the "
                    "path must match exactly. Must start with '/'.")
    to: str = Field(
        description="Where to send it: a path ('/new-page') or an absolute URL "
                    "('https://example.com/new'). Placeholders from `from` are "
                    "substituted — '/blog/*' -> '/news/:splat', '/posts/:id' -> "
                    "'/articles/:id'. The incoming query string is carried over "
                    "unless this sets its own.")
    status: int = Field(
        default=301,
        description="301 permanent (the default, and what search engines act on), "
                    "302 temporary, 308/307 the method-preserving equivalents — "
                    "use 308 when a redirected POST must stay a POST.")

    model_config = {"populate_by_name": True}


class Redirects(BaseModel):
    redirects: list[Redirect] = Field(
        description="The app's complete redirect list, in priority order — the "
                    "FIRST match wins, so put specific rules before catch-alls. "
                    "This replaces the whole list, so include the rules you are "
                    "keeping; send [] to remove them all.")


def _validate(rules: list[Redirect]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for r in rules:
        src, dst = r.from_.strip(), r.to.strip()
        if not src.startswith("/"):
            raise HTTPException(422, f"'from' must start with '/': {src}")
        if not dst:
            raise HTTPException(422, f"'to' is empty for {src}")
        if r.status not in STATUSES:
            raise HTTPException(
                422, f"status must be one of {STATUSES}, got {r.status}")
        if src in seen:
            raise HTTPException(
                422, f"duplicate rule for {src} — the first would always win")
        # A target placeholder with no matching source capture would render
        # literally in the Location header, which is never what was meant.
        names = set(_PLACEHOLDER.findall(src)) | ({"splat"} if "*" in src else set())
        unknown = set(_PLACEHOLDER.findall(dst)) - names
        if unknown:
            raise HTTPException(
                422, f"{src} -> {dst}: ':{', :'.join(sorted(unknown))}' is not "
                     f"captured by the 'from' pattern")
        if src == dst:
            raise HTTPException(422, f"{src} redirects to itself")
        seen.add(src)
        out.append({"from": src, "to": dst, "status": r.status})
    return out


async def _apply(c, app_id: str) -> dict:
    """Push the app's rules into the manifest the static host reads, and make
    sure its hostname is routed there (an app can have redirects without a
    frontend)."""
    row = await c.fetchrow(
        "SELECT image, redirects, spa FROM apps WHERE id = $1", app_id)
    rules = list(row["redirects"] or [])
    frontends.write_manifest(app_id, bool(row["image"]), rules,
                             spa=bool(row["spa"]))
    try:
        routed = await routing.sync_frontend_routes(c)
    except Exception as e:
        routed = {"error": str(e)}
    return {"redirects": rules, "routing": routed}


@router.get("/{app_id}/redirects", operation_id="apps_redirects_list",
            summary="List an app's redirect rules, in priority order")
async def list_redirects(app_id: str):
    """The app's redirect rules in the order they are evaluated — the first match
    wins. Read this before setting rules, since apps_redirects_set replaces the
    whole list."""
    async with pool().acquire() as c:
        row = await c.fetchrow("SELECT redirects FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app")
    return {"id": app_id, "redirects": list(row["redirects"] or [])}


@router.put("/{app_id}/redirects", operation_id="apps_redirects_set",
            summary="Set an app's redirect rules (replaces the whole list)")
async def set_redirects(app_id: str, body: Redirects):
    """Replace an app's redirect rules.

    This is the create, update and delete path: send the full list you want. To
    add one rule, call apps_redirects_list first and send the existing rules plus
    the new one; to delete one, send the list without it; to remove them all,
    send an empty list.

    Rules are evaluated in order and the first match wins, so specific rules must
    come before catch-alls — `/blog/*` placed first would swallow
    `/blog/special`. They apply to every request for the app, ahead of static
    files and the backend, and take effect immediately with no redeploy.

    Patterns: `*` matches the rest of the path and is available in the target as
    `:splat`; `:name` matches one segment and is available as `:name`. Targets
    may be paths or absolute URLs, and the incoming query string is preserved.
    """
    rules = _validate(body.redirects)
    async with pool().acquire() as c:
        if not await c.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
            raise HTTPException(404, "no such app")
        # Pass the list itself: the pool registers a jsonb codec, so asyncpg
        # encodes it. json.dumps() here would encode it twice and store the JSON
        # *string* "[]" rather than an empty array — which reads back as a string
        # and iterates into ['[', ']'].
        await c.execute("UPDATE apps SET redirects = $1 WHERE id = $2",
                        rules, app_id)
        applied = await _apply(c, app_id)
    return {"id": app_id, "count": len(rules), **applied}
