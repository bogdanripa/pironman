"""Database provisioning — thin wrapper around the host `pdb` script.

`pdb` is mounted read-only into this container and invoked through the Docker
CLI, which talks to the mounted socket. Keeping one implementation on the host
means the script stays usable by hand for debugging.
"""
import asyncio
import json
import re
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
