"""host_run_script — a shell on the Pi itself. See app/hostexec.py for how the
control plane gets out of its own container to do it."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_key
from .. import hostexec

router = APIRouter(prefix="/host", tags=["host"],
                   dependencies=[Depends(require_key)])


class Script(BaseModel):
    script: str = Field(
        description=(
            "A shell script, run by /bin/sh on the Raspberry Pi as root, in the "
            "host's own filesystem and network — not inside any app's container. "
            "Multiple lines allowed; the working directory starts at /. Execution "
            "does NOT stop at the first failing command, so begin with `set -e` "
            "if it should. e.g. \"df -h /\" or \"set -e\\ncd /opt/paas\\nls -la\""))
    timeout: int = Field(
        default=60, ge=1, le=600,
        description="Seconds before the script is killed on the box. Raise it "
                    "for a long apt upgrade or a large copy. For anything longer "
                    "than the 600s ceiling, start it in the background "
                    "(nohup … &) and poll its log with a second call.")


@router.post("/run", operation_id="host_run_script",
             summary="Run a shell script on the Raspberry Pi host itself")
async def host_run_script(body: Script):
    """Execute a shell script on the box and return its combined stdout/stderr.

    This is the host, not a container: `docker ps` lists every container on the
    Pi, `df -h` reports the real disk, `/etc` and `/boot/firmware` are the Pi's
    own, `systemctl` drives host services, and `crontab -l` shows the dispatcher.
    It runs as **root**. Use it for the things that have no tool of their own —
    disk pressure and cleanup, kernel/OS config, inspecting a container the
    platform did not create, reading a host log, checking `uptime` or
    temperature.

    Prefer a purpose-built tool where one exists: apps_logs for an app's logs,
    apps_stats for CPU/RAM/disk/health, db_run_script for an app's database.
    Those are safer, cheaper and give better-shaped answers than shelling out.

    **There are no guardrails.** `rm -rf`, a package removal, a `docker rm` or a
    reboot will execute exactly as written, and this is the machine the whole
    platform runs on — including this API. Read the script back to the user and
    confirm before running anything that writes, removes, installs or restarts.
    Read-only commands are safe to run directly.

    Returns `exit_code` (the script's last command, or 124 if it hit the timeout)
    and `output` — raw text, truncated past ~100k characters. A non-zero
    `exit_code` is a failed script, not a failed call: the response is still 200.
    """
    try:
        code, out = await hostexec.run_script(body.script, body.timeout)
    except hostexec.HostExecError as e:
        raise HTTPException(400, str(e))
    return {"exit_code": code, "output": out}
