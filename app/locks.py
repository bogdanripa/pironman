"""Serialisation for operations that redeploy something.

Coolify's deploy call is asynchronous, so two overlapping calls for one app do
not queue politely: the second is started against a config the first is still
changing, and which container survives is a coin toss. That is not hypothetical
— an app whose CI ships a frontend and a backend runs both jobs at once, so the
frontend upload (which can rewrite routing labels and redeploy) lands in the
middle of the backend's image swap.

There is exactly one control-plane process, so an in-process lock is enough. Two
scopes:

  - `app_lock(id)` — taken at API entry points that redeploy one app.
  - `ROUTING_LOCK` — taken inside routing.sync_frontend_routes, which touches the
    shared static host and therefore every app at once.

Locks are only ever taken at the outermost layer. Nothing that runs while holding
one takes another for the same app: asyncio.Lock is not reentrant, and a deploy
path that deadlocks is worse than one that races.
"""
import asyncio

_locks: dict[str, asyncio.Lock] = {}

# Guards the shared static host's label set, which is derived from every app.
ROUTING_LOCK = asyncio.Lock()


def app_lock(app_id: str) -> asyncio.Lock:
    """The lock for one app. Created on first use and kept — there are tens of
    apps on this box, not thousands, so nothing needs evicting."""
    lock = _locks.get(app_id)
    if lock is None:
        lock = _locks[app_id] = asyncio.Lock()
    return lock
