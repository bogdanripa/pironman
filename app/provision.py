"""Database provisioning — thin wrapper around the host `pdb` script.

`pdb` is mounted read-only into this container and invoked through the Docker
CLI, which talks to the mounted socket. Keeping one implementation on the host
means the script stays usable by hand for debugging.
"""
import asyncio
import json
import re
import secrets
from urllib.parse import quote

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")

PORTS = {"postgres": 5432, "mongo": 27017}


class ProvisionError(RuntimeError):
    pass


async def _pdb(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "/usr/local/bin/pdb", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise ProvisionError(err.decode().strip() or "pdb failed")
    return out.decode().strip()


async def create(name: str, engine: str) -> dict:
    """Returns {engine, host, port, user, password, database}."""
    raw = await _pdb("create", name, "--engine", engine)
    return json.loads(raw)


async def drop(name: str, engine: str) -> None:
    await _pdb("drop", name, "--engine", engine)


async def current_host(engine: str) -> str:
    """Live container name. Changes whenever Coolify recreates the resource,
    which is exactly why we never persist a composed URL."""
    return await _pdb("host", "--engine", engine)


async def compose_url(engine: str, user: str, password: str, database: str) -> str:
    host = await current_host(engine)
    scheme = "postgresql" if engine == "postgres" else "mongodb"
    return (
        f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{PORTS[engine]}/{database}"
    )


async def db_size(engine: str, database: str, user: str, password: str,
                  timeout: int = 15) -> int | None:
    """On-disk size of one app's database, in bytes (None if it can't be read).

    Same `docker exec` path as run_script, but asks the engine for its own size:
    Postgres pg_database_size(), Mongo db.stats().storageSize. Read-only.
    """
    host = await current_host(engine)
    if engine == "postgres":
        cmd = [
            "docker", "exec", "-i", "-e", f"PGPASSWORD={password}", host,
            "psql", "-U", user, "-d", database, "-tAc",
            "SELECT pg_database_size(current_database())",
        ]
    else:
        cmd = [
            "docker", "exec", "-i", host, "mongosh", "--quiet",
            "-u", user, "-p", password, "--authenticationDatabase", database,
            database, "--eval", "JSON.stringify(db.stats())",
        ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(b""), timeout + 10)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0:
        return None

    text = out.decode().strip()
    if engine == "postgres":
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.isdigit():
                return int(line)
        return None
    # Mongo: the eval prints the JSON string; storageSize is the on-disk figure.
    try:
        stats = json.loads(text.splitlines()[-1])
    except (ValueError, IndexError):
        return None
    val = stats.get("storageSize") or stats.get("dataSize")
    return int(val) if val is not None else None


async def run_script(engine: str, database: str, user: str, password: str,
                     script: str, timeout: int = 30) -> str:
    """Raw SQL / Mongo script against one app's database. Deliberately
    unguarded per design, with a statement timeout as the only backstop."""
    host = await current_host(engine)
    if engine == "postgres":
        cmd = [
            "docker", "exec", "-i",
            "-e", f"PGPASSWORD={password}",
            "-e", f"PGOPTIONS=-c statement_timeout={timeout * 1000}",
            host, "psql", "-U", user, "-d", database, "-v", "ON_ERROR_STOP=1",
        ]
        stdin = script.encode()
    else:
        cmd = [
            "docker", "exec", "-i", host, "mongosh", "--quiet",
            "-u", user, "-p", password,
            "--authenticationDatabase", database, database,
        ]
        stdin = script.encode()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout + 10)
    except asyncio.TimeoutError:
        proc.kill()
        raise ProvisionError("script timed out")
    return out.decode()


# The read-only role's name is derived, never chosen: one per app, so a leaked
# credential names exactly one database and the tool that uses it cannot be
# pointed anywhere else.
def ro_user(name: str) -> str:
    return f"{name}_ro"


async def _as_superuser(engine: str, sql: str, database: str | None = None,
                        timeout: int = 30) -> str:
    """Run one statement batch as the engine's own superuser.

    Needed because granting is the owner's prerogative and the app's user cannot
    create a role. Deliberately NOT reachable from any tool — the only callers
    are the provisioning paths in this module.
    """
    host = await current_host(engine)
    if engine == "postgres":
        cmd = ["docker", "exec", "-i",
               "-e", f"PGOPTIONS=-c statement_timeout={timeout * 1000}",
               host, "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1",
               "-d", database or "postgres"]
    else:
        cmd = ["docker", "exec", "-i", host, "mongosh", "--quiet"]
        u = await _mongo_root(host)
        if u:
            cmd = ["docker", "exec", "-i", host, "mongosh", "--quiet",
                   "-u", u[0], "-p", u[1], "--authenticationDatabase", "admin"]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(sql.encode()),
                                        timeout + 10)
    except asyncio.TimeoutError:
        proc.kill()
        raise ProvisionError("privileged statement timed out")
    text = out.decode()
    if proc.returncode != 0:
        raise ProvisionError(text.strip()[:400] or "privileged statement failed")
    return text


async def _mongo_root(host: str) -> tuple[str, str] | None:
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", host, "printenv", "MONGO_INITDB_ROOT_USERNAME",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    user = out.decode().strip()
    if not user:
        return None
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", host, "printenv", "MONGO_INITDB_ROOT_PASSWORD",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return user, out.decode().strip()


async def ensure_readonly(name: str, engine: str,
                          password: str | None = None) -> str:
    """Create (or re-grant) a read-only login for one app's database, and return
    its password.

    **The restriction is the database's, not the tool's.** Nothing here inspects
    the SQL a caller sends, because a "SELECT-only" check is the kind of
    guardrail that reads as security and is not: `WITH x AS (DELETE FROM t
    RETURNING *) SELECT * FROM x` starts with the wrong word, a function can
    write, and a statement can be spelled a dozen ways. An engine-enforced
    privilege has none of those edges — it does not care how the write is
    spelled.

    Two layers, and it matters which one is load-bearing:

      1. **The grants are the boundary.** The role holds SELECT and nothing
         else, so a write has no privilege to use, and no amount of session
         fiddling creates one. Measured on this box 2026-09-24 against
         PostgreSQL 18.4: INSERT/UPDATE/DELETE and a DELETE hidden in a CTE all
         answer `permission denied for table …`, DROP answers `must be owner`,
         and CREATE TABLE answers `permission denied for schema public`.
      2. `default_transaction_read_only = on` on the role is defence in depth,
         NOT a second wall. A caller can turn it off — `SET
         default_transaction_read_only = off` succeeds, and because run_script
         pipes the script to psql on stdin each statement runs in its own
         implicit transaction, so the setting takes effect for the ones that
         follow. It was verified failing exactly that way. What it still buys
         is a clearer error for an honest mistake, and a backstop if a SELECT
         grant is ever widened by accident — it does not stop someone who
         means it, and it must not be described as though it does.

    So the thing to protect is the grant list. Widening it is what would make
    this tool writable; the transaction setting would not save it.

    Idempotent: re-applies grants every time, which is what picks up tables
    created since the role was made. ALTER DEFAULT PRIVILEGES covers what the
    app's own user creates from here on; the blanket GRANT covers what already
    exists and anything created by another owner.
    """
    password = password or secrets.token_hex(24)
    ro = ro_user(name)
    if engine == "postgres":
        # Role first, in the maintenance database; grants second, inside the
        # app's own database, because that is where the schema lives.
        await _as_superuser("postgres", f"""
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{ro}') THEN
                CREATE ROLE "{ro}" LOGIN;
              END IF;
            END $$;
            ALTER ROLE "{ro}" WITH PASSWORD '{password}'
              NOSUPERUSER NOCREATEDB NOCREATEROLE;
            ALTER ROLE "{ro}" SET default_transaction_read_only = on;
            GRANT CONNECT ON DATABASE "{name}" TO "{ro}";
        """)
        await _as_superuser("postgres", f"""
            GRANT USAGE ON SCHEMA public TO "{ro}";
            GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{ro}";
            ALTER DEFAULT PRIVILEGES FOR ROLE "{name}" IN SCHEMA public
              GRANT SELECT ON TABLES TO "{ro}";
        """, database=name)
    else:
        # Mongo's built-in `read` role is exactly this, scoped to one database.
        await _as_superuser("mongo", f"""
            db = db.getSiblingDB({name!r});
            if (db.getUser({ro!r})) {{
              db.updateUser({ro!r}, {{pwd: {password!r},
                                      roles: [{{role: 'read', db: {name!r}}}]}});
            }} else {{
              db.createUser({{user: {ro!r}, pwd: {password!r},
                              roles: [{{role: 'read', db: {name!r}}}]}});
            }}
        """)
    return password
