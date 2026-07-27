"""paas-key — issue API keys. Run inside the container:
      python -m app.cli create "bootstrap"
The token is printed once and only its sha256 is stored."""
import asyncio, hashlib, secrets, sys
from .db import init_pool, close_pool, pool


async def create(label: str) -> None:
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    await init_pool()
    async with pool().acquire() as c:
        await c.execute(
            "INSERT INTO api_keys (key_hash, label) VALUES ($1, $2)", digest, label)
    await close_pool()
    print(token)


async def ls() -> None:
    await init_pool()
    async with pool().acquire() as c:
        rows = await c.fetch("SELECT id, label, created_at, last_used FROM api_keys")
    await close_pool()
    for r in rows:
        print(f"{r['id']}  {r['label'] or '-':20}  last_used={r['last_used']}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "create":
        asyncio.run(create(sys.argv[2] if len(sys.argv) > 2 else "unnamed"))
    elif len(sys.argv) >= 2 and sys.argv[1] == "list":
        asyncio.run(ls())
    else:
        print("usage: python -m app.cli <create <label>|list>", file=sys.stderr)
        sys.exit(1)
