"""Shell scripts on the Pi itself, not inside an app container.

The control plane is a container, so "on the host" takes a deliberate escape.
The one surface it has to the box is the mounted Docker socket, so the script is
run by a throwaway container that shares the host PID namespace and `nsenter`s
into PID 1's mount/UTS/IPC/network/PID namespaces. What comes out the other side
is a plain `sh` running in the host's own namespaces: the host's filesystem, the
host's network, the host's processes — exactly what you would get over SSH.

The helper container is a vehicle for the `nsenter` binary and nothing else. The
script never touches its filesystem, which is why the image barely matters (see
`_helper_image`) — and why running this does not consume an app slot, appear in
`apps_list`, or leave anything behind.
"""
import asyncio
import secrets

from .config import CONTROL_PLANE_APP, HOST_EXEC_IMAGE
from .db import pool

# Enough to read a log tail or a directory listing, small enough that a runaway
# `find /` cannot blow up the MCP response. Truncation is reported, never silent.
MAX_OUTPUT = 100_000


class HostExecError(RuntimeError):
    pass


_image: str | None = None


async def _helper_image() -> str:
    """Which image carries the `nsenter` we borrow.

    Any image with `nsenter` works, so the default is the control plane's own:
    it is by definition already on this box (it is what is running this code),
    so the tool never waits on a registry pull, and its Debian base ships
    nsenter in util-linux. HOST_EXEC_IMAGE overrides it; `alpine` is the last
    resort for a box where the control plane has not adopted itself yet.
    """
    global _image
    if HOST_EXEC_IMAGE:
        return HOST_EXEC_IMAGE
    if _image:
        return _image
    async with pool().acquire() as c:
        img = await c.fetchval("SELECT image FROM apps WHERE id = $1",
                               CONTROL_PLANE_APP)
    _image = img or "alpine"
    return _image


async def run_script(script: str, timeout: int = 60) -> tuple[int, str]:
    """Run `script` on the host with `sh`. Returns (exit code, combined output).

    Two timeouts, because they fail differently. The host-side `timeout` bounds
    the script itself and leaves the helper container to exit cleanly. The outer
    wait_for is the backstop for the case where that never happens — a wedged
    daemon, a process ignoring SIGKILL — and it force-removes the container,
    since killing the `docker run` client would otherwise leave it running.
    """
    image = await _helper_image()
    name = f"pironman-host-{secrets.token_hex(6)}"

    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm", "-i", "--name", name,
        # --pid=host is what makes PID 1 below the *host's* init rather than
        # this helper's; --privileged is what allows setns into its namespaces.
        "--privileged", "--pid=host", image,
        "nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--",
        "timeout", "-k", "5", str(timeout),
        # nsenter hands the script the *container's* environment, which is this
        # control plane's — its secrets, and a PATH that need not include the
        # host's sbin dirs. `env -i` replaces it with a plain root login's, so
        # the script sees the box and not the container that launched it.
        # (nsenter's own --wd is no help: it resolves the directory before
        # entering the mount namespace, so it would land on the container's /.
        # `cd /` inside the shell runs after the switch and means the host's.)
        "env", "-i", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
                     ":/sbin:/bin", "HOME=/root", "TERM=dumb",
        # The script itself arrives on stdin rather than as an argument — no
        # quoting to get wrong, no argument-length limit — which is why the
        # shell that runs it is exec'd rather than given a -c command.
        "/bin/sh", "-c", "cd / && exec /bin/sh",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(script.encode()),
                                        timeout + 15)
    except asyncio.TimeoutError:
        proc.kill()
        await (await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)).wait()
        raise HostExecError(
            f"script did not finish within {timeout}s and did not stop when "
            "asked — the helper container was force-removed")

    text = out.decode(errors="replace")
    if len(text) > MAX_OUTPUT:
        text = (text[:MAX_OUTPUT]
                + f"\n… truncated at {MAX_OUTPUT} characters "
                  "(redirect to a file on the box and read it back in pieces)")
    # The `docker run` client prefixes its own failures — a missing image, an
    # image without nsenter — with "docker:". Those are the helper failing to
    # start, not the script failing, and returning them as script output would
    # send the caller off debugging a script that never ran.
    if proc.returncode != 0 and text.lstrip().startswith(("docker:",
                                                          "Unable to find image")):
        raise HostExecError(
            f"could not start the host helper ({image}): {text.strip()}")
    return proc.returncode, text
