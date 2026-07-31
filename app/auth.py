import hashlib
import secrets

from fastapi import Header, HTTPException, Request

from .db import pool


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def mint_key(conn, label: str, app_id: str | None = None) -> str:
    """Create an API key and return the plaintext token (shown once; only its
    sha256 is stored). Pass app_id to scope the key to a single app — such a key
    may only redeploy that app, see require_key."""
    token = secrets.token_urlsafe(32)
    await conn.execute(
        "INSERT INTO api_keys (key_hash, label, app_id) VALUES ($1, $2, $3)",
        _hash(token), label, app_id)
    return token


async def require_key(request: Request, authorization: str = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "UPDATE api_keys SET last_used = now() WHERE key_hash = $1 "
            "RETURNING id, label, app_id",
            _hash(token),
        )
    if not row:
        raise HTTPException(401, "invalid api key")

    # A per-app deploy key (app_id set) is least-privilege: it may only ship its
    # own app — trigger its backend deploy (POST /apps/<id>/refresh), redeploy it
    # explicitly (PUT /apps/<id>/code) or upload its frontend bundle
    # (PUT /apps/<id>/frontend) — and nothing else, on no other app. Unscoped keys
    # (app_id NULL) are admin keys and pass through. Enforced here, centrally, so
    # no endpoint can forget the check.
    _DEPLOY_ROUTES = {
        ("PUT", "/apps/{app_id}/code"),
        ("PUT", "/apps/{app_id}/frontend"),
        ("POST", "/apps/{app_id}/refresh"),
    }
    if row["app_id"] is not None:
        route = request.scope.get("route")
        route_path = getattr(route, "path", "")
        target = request.path_params.get("app_id")
        if not ((request.method, route_path) in _DEPLOY_ROUTES
                and target == row["app_id"]):
            raise HTTPException(
                403, "this key is scoped to a single app and can only deploy that "
                     "app (POST /apps/<id>/refresh, PUT /apps/<id>/code or "
                     "PUT /apps/<id>/frontend)")

    return row["label"] or str(row["id"])
