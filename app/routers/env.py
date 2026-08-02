"""Environment variables — two scopes, two routers.

Shared variables (tag ``env``) apply to every app; per-app variables (tag
``apps``) apply to one and override a shared key of the same name. Both are
stored in the _paas registry and pushed into Coolify; a change redeploys the
affected app(s) so it actually reaches the running container.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_key
from ..db import pool
from ..locks import app_lock
from .. import apperr, coolify, envs

# Columns sync_env needs to recompose an app's environment and redeploy it.
_APP_COLS = ("id, coolify_uuid, has_frontend, db_engine, db_user, db_password, db_name")

shared_router = APIRouter(prefix="/env", tags=["env"],
                          dependencies=[Depends(require_key)])
app_router = APIRouter(prefix="/apps", tags=["apps"],
                       dependencies=[Depends(require_key)])


class EnvValue(BaseModel):
    value: str = Field(
        description="The variable's value, e.g. an API key. Stored and injected "
                    "into the container's environment on deploy. Write-only: it "
                    "is never read back in plaintext — listings show a masked "
                    "preview only, so overwrite it if you are unsure rather than "
                    "trying to read it.")
    redeploy: bool = Field(
        default=True,
        description="Redeploy the affected app(s) so the change takes effect "
                    "immediately. A Coolify env change is inert until the next "
                    "deploy, so leaving this true is what makes the variable "
                    "actually reach the running container. Set false to stage "
                    "several changes and let the next code deploy pick them up.")


# Only an app with a container has an environment to push into. A frontend-only
# app has no Coolify application at all, so every call here would 404 against
# uuid NULL — and a shared-variable change, which walks every app, would abort
# partway through on the first one it met.
_WITH_CONTAINER = "coolify_uuid IS NOT NULL"


async def _apply(conn, row, redeploy: bool) -> None:
    """Recompose one app's full environment in Coolify, then optionally deploy.

    Serialised per app: an env change redeploys, and two overlapping Coolify
    deploys of one app fight rather than queue."""
    async with app_lock(row["id"]):
        await envs.sync_env(conn, row["coolify_uuid"], row["id"], row["db_engine"],
                            row["db_user"], row["db_password"], row["db_name"])
        if redeploy:
            await coolify.deploy(
                row["coolify_uuid"], app_id=row["id"],
                reason="environment variables changed")


# --- shared: env_* ---------------------------------------------------------

@shared_router.get("", operation_id="env_list",
                   summary="List shared environment variables (applied to every app)")
async def env_list():
    """Every account-wide variable, with a masked preview and when it changed.

    These are injected into all apps — the place for cross-cutting secrets like
    an OpenAI API key. Values are never returned in plaintext.
    """
    async with pool().acquire() as c:
        rows = await c.fetch(
            "SELECT key, value, updated_at FROM shared_env ORDER BY key")
    return [{"key": r["key"], "preview": envs.mask(r["value"]),
             "updated_at": r["updated_at"]} for r in rows]


@shared_router.put("/{key}", operation_id="env_set",
                   summary="Set a shared variable and roll it out to every app")
async def env_set(key: str, body: EnvValue):
    """Create or overwrite an account-wide variable and inject it into every app.

    Use this for anything shared across apps — an OpenAI key, a shared webhook
    secret. An app that sets its own variable of the same name keeps its own
    value; this does not override those.

    With redeploy=true (the default) every app is redeployed so the value is
    live immediately. On a box with several apps that is several redeploys, so
    set redeploy=false if you are staging a batch of changes.
    """
    try:
        envs.validate_key(key)
    except envs.EnvError as e:
        raise HTTPException(422, str(e))

    async with pool().acquire() as c:
        await c.execute(
            "INSERT INTO shared_env (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()",
            key, body.value)
        apps = await c.fetch(
            f"SELECT {_APP_COLS} FROM apps WHERE {_WITH_CONTAINER} ORDER BY id")
        for app in apps:
            await _apply(c, app, body.redeploy)

    return {"key": key, "preview": envs.mask(body.value),
            "applied_to": [a["id"] for a in apps],
            "redeployed": body.redeploy and bool(apps)}


@shared_router.delete("/{key}", status_code=200, operation_id="env_delete",
                      summary="Remove a shared variable from every app")
async def env_delete(key: str, redeploy: bool = True):
    """Delete an account-wide variable and remove it from every app.

    An app that defined its own variable of the same name keeps that value — its
    override survives, only the shared default goes away.
    """
    async with pool().acquire() as c:
        gone = await c.execute("DELETE FROM shared_env WHERE key = $1", key)
        if gone.endswith("0"):
            raise HTTPException(404, "no such shared variable")
        apps = await c.fetch(
            f"SELECT {_APP_COLS} FROM apps WHERE {_WITH_CONTAINER} ORDER BY id")
        for app in apps:
            # Drop it, then re-sync: an app with its own override gets that value
            # back, an app without one is left without the key.
            await coolify.delete_env(app["coolify_uuid"], key)
            await _apply(c, app, redeploy)

    return {"key": key, "removed_from": [a["id"] for a in apps],
            "redeployed": redeploy and bool(apps)}


# --- per-app: apps_env_* ---------------------------------------------------

async def _require_app_row(conn, app_id: str, needs_container: bool = False):
    row = await conn.fetchrow(f"SELECT {_APP_COLS} FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app")
    if needs_container and not row["coolify_uuid"]:
        raise HTTPException(400, apperr.no_container(
            app_id, row, "it has no environment to set",
            "Put configuration in the bundle, or give the app a backend."))
    return row


@app_router.get("/{app_id}/env", operation_id="apps_env_list",
                summary="List one app's environment variables, own and inherited")
async def apps_env_list(app_id: str):
    """What this app runs with: its own variables plus the shared ones it
    inherits, each with a masked preview. A key that appears in both is shown
    under `env` (the app's own value wins). DATABASE_URL is managed separately
    and is not listed here.
    """
    async with pool().acquire() as c:
        await _require_app_row(c, app_id)
        own = await c.fetch(
            "SELECT key, value, updated_at FROM app_env WHERE app_id = $1 "
            "ORDER BY key", app_id)
        own_keys = {r["key"] for r in own}
        shared = await c.fetch("SELECT key, value FROM shared_env ORDER BY key")
    return {
        "app_id": app_id,
        "env": [{"key": r["key"], "preview": envs.mask(r["value"]),
                 "updated_at": r["updated_at"]} for r in own],
        "inherited_shared": [{"key": r["key"], "preview": envs.mask(r["value"])}
                             for r in shared if r["key"] not in own_keys],
    }


@app_router.put("/{app_id}/env/{key}", operation_id="apps_env_set",
                summary="Set an environment variable on one app")
async def apps_env_set(app_id: str, key: str, body: EnvValue):
    """Create or overwrite a variable on a single app. If a shared variable of
    the same name exists, this value overrides it for this app only.

    With redeploy=true (the default) the app is redeployed so the value is live.
    """
    try:
        envs.validate_key(key)
    except envs.EnvError as e:
        raise HTTPException(422, str(e))

    async with pool().acquire() as c:
        row = await _require_app_row(c, app_id, needs_container=True)
        await c.execute(
            "INSERT INTO app_env (app_id, key, value) VALUES ($1, $2, $3) "
            "ON CONFLICT (app_id, key) DO UPDATE SET value = $3, updated_at = now()",
            app_id, key, body.value)
        await _apply(c, row, body.redeploy)

    return {"app_id": app_id, "key": key, "preview": envs.mask(body.value),
            "redeployed": body.redeploy}


@app_router.delete("/{app_id}/env/{key}", status_code=200,
                   operation_id="apps_env_delete",
                   summary="Remove an environment variable from one app")
async def apps_env_delete(app_id: str, key: str, redeploy: bool = True):
    """Delete one of an app's own variables. If a shared variable of the same
    name exists, the app falls back to that shared value rather than losing the
    key entirely.
    """
    async with pool().acquire() as c:
        row = await _require_app_row(c, app_id, needs_container=True)
        gone = await c.execute(
            "DELETE FROM app_env WHERE app_id = $1 AND key = $2", app_id, key)
        if gone.endswith("0"):
            raise HTTPException(404, "no such variable on this app")
        # Drop it, then re-sync so a shared default of the same name comes back.
        await coolify.delete_env(row["coolify_uuid"], key)
        await _apply(c, row, redeploy)

    return {"app_id": app_id, "key": key, "redeployed": redeploy}
