import hashlib
from fastapi import Header, HTTPException
from .db import pool

async def require_key(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    digest = hashlib.sha256(token.encode()).hexdigest()
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "UPDATE api_keys SET last_used = now() WHERE key_hash = $1 "
            "RETURNING id, label",
            digest,
        )
    if not row:
        raise HTTPException(401, "invalid api key")
    return row["label"] or str(row["id"])
