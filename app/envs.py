"""Environment variables for apps.

Two scopes, both stored in the _paas registry:

  shared_env  account-wide variables injected into every app — this is your
              box and your account, so an OpenAI key or similar belongs here
              rather than being pasted into each app.
  app_env     per-app variables. An app-specific key overrides a shared key of
              the same name, so one app can point at a different value.

The *desired* environment for an app is therefore (shared, overlaid by
app-specific). DATABASE_URL is not part of this — it is managed by the platform
and composed fresh on every deploy — so it is reserved and cannot be set here.

Values are write-only: they are pushed into Coolify but never read back in
plaintext through the API. Listings show a masked preview only, so a secret
never lands in a tool result or a chat transcript.
"""
import re

from . import coolify, provision

ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

# Managed by the platform (see provision.compose_url) — never user-settable.
RESERVED = {"DATABASE_URL"}


class EnvError(ValueError):
    """Rejected env name; routers translate this to HTTP 422."""


def validate_key(key: str) -> None:
    if not ENV_KEY_RE.match(key):
        raise EnvError(
            "env name must match ^[A-Za-z_][A-Za-z0-9_]* (letters, digits and "
            "underscores, not starting with a digit)")
    if key in RESERVED:
        raise EnvError(f"{key} is managed by the platform and cannot be set")


def mask(value: str) -> str:
    """A confirmation that a value is set, without echoing the secret."""
    return f"•••• ({len(value)} chars)"


async def desired_env(conn, app_id: str) -> dict[str, str]:
    """The full set of variables an app should run with: every shared variable,
    overlaid by the app's own, which win on a name clash. DATABASE_URL is added
    separately at deploy time and is not included here."""
    shared = await conn.fetch("SELECT key, value FROM shared_env")
    own = await conn.fetch("SELECT key, value FROM app_env WHERE app_id = $1", app_id)
    env = {r["key"]: r["value"] for r in shared}
    env.update({r["key"]: r["value"] for r in own})
    return env


async def sync_env(conn, uuid: str, app_id: str, db_engine, db_user,
                   db_password, db_name) -> None:
    """Push an app's full desired env into Coolify: every shared/app variable,
    plus a freshly composed DATABASE_URL when the app has a database. Idempotent
    — it upserts, so calling it on every deploy keeps the container's env in
    step with the registry. It does not deploy; the caller decides when to."""
    for key, value in (await desired_env(conn, app_id)).items():
        await coolify.set_env(uuid, key, value)
    if db_engine:
        url = await provision.compose_url(db_engine, db_user, db_password, db_name)
        await coolify.set_env(uuid, "DATABASE_URL", url)
