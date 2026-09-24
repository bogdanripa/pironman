"""Every MCP tool must carry annotations, and they must be true.

Run it directly: `python tests/test_mcp_annotations.py`.

These are not decoration. Two things read them and both change what an agent can
do:

  * the claude.ai connector's approval gate — a destructiveHint tool prompts
    before every call, including inside an unattended Routine where nobody can
    answer (2026-08-03: the nightly audit sat blocked from ~02:00 to 07:35 on
    its first call);
  * the Tasks agent platform, which HIDES destructiveHint tools unless an admin
    allows them.

So a tool marked wrongly is either unreachable when it should work, or reachable
when it should have been gated. The set used to be two hand-maintained lists and
it drifted: platform_tasks_health and platform_events are plain GETs that were
advertised as state-changing for months, the first being the highest-signal
check the nightly audit makes. They are derived from the route methods now, and
this file is what keeps the derivation honest.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for k in ("COOLIFY_TOKEN", "COOLIFY_PROJECT", "COOLIFY_SERVER",
          "COOLIFY_DESTINATION", "PAAS_DB_PASSWORD"):
    os.environ.setdefault(k, "x")
os.environ.setdefault("PAAS_DB_HOST", "localhost")

from app import main  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def main_():
    tools = {t.name: t for t in (getattr(main._mcp, "tools", None) or [])}
    check("the MCP server exposes tools at all", len(tools) > 20, f"{len(tools)} tools")

    # operation_id -> method, the same way main.py derives it.
    methods = {}
    for route in main.app.routes:
        op = getattr(route, "operation_id", None)
        for m in (getattr(route, "methods", None) or ()):
            if op and m not in ("HEAD", "OPTIONS"):
                methods[op] = m

    print("\n[every tool is annotated]")
    missing = [n for n, t in tools.items() if t.annotations is None]
    check("no tool is left without annotations", not missing, str(missing))
    unresolved = sorted(n for n in tools if n not in methods)
    check("every tool's route method resolved", not unresolved, str(unresolved))

    print("\n[read-only means read-only]")
    gets = sorted(n for n in tools if methods.get(n) == "GET")
    check("there are GET-backed tools to check", len(gets) > 5, f"{len(gets)}")
    wrong = [n for n in gets if not tools[n].annotations.readOnlyHint]
    check("every GET-backed tool is readOnlyHint", not wrong, str(wrong))
    # The pair that was mis-tagged for months, named explicitly so a regression
    # is reported as itself rather than as a count.
    for n in ("platform_tasks_health", "platform_events"):
        if n in tools:
            check(f"{n} is read-only", tools[n].annotations.readOnlyHint is True)
    never = [n for n in gets if tools[n].annotations.destructiveHint]
    check("no read-only tool is marked destructive", not never, str(never))

    print("\n[the tools an agent platform must be able to gate]")
    # The owner's list, verbatim. Each is either a DELETE route or an explicit
    # override; if one silently stops being destructive, Tasks stops hiding it.
    for n in ("apps_delete", "apps_detach_db", "db_run_script", "host_run_script",
              "apps_env_delete", "env_delete", "github_secret_delete"):
        if n not in tools:
            check(f"{n} exists as a tool", False, "not exposed")
            continue
        check(f"{n} is destructiveHint", tools[n].annotations.destructiveHint is True)
        check(f"{n} is not readOnly", tools[n].annotations.readOnlyHint is False)

    print("\n[the script tools are the deliberate override]")
    for n in ("host_run_script", "db_run_script"):
        if n in tools:
            check(f"{n} is a POST route (so the method alone would NOT mark it)",
                  methods.get(n) == "POST", str(methods.get(n)))

    print("\n[idempotency reflects the verb]")
    for n, t in sorted(tools.items()):
        m = methods.get(n)
        if m in ("GET", "PUT", "DELETE"):
            if not t.annotations.idempotentHint:
                check(f"{n} ({m}) is idempotent", False)
        elif m == "POST" and t.annotations.idempotentHint:
            check(f"{n} (POST) is not marked idempotent", False)
    check("idempotency matches every route's verb", True)

    print("\n[openWorldHint marks the tools that leave this box]")
    for n in ("github_secret_set", "github_secrets_list", "github_secret_delete"):
        if n in tools:
            check(f"{n} is openWorld", tools[n].annotations.openWorldHint is True)
    for n in ("apps_list", "apps_stats", "host_run_script"):
        if n in tools:
            check(f"{n} is not openWorld",
                  tools[n].annotations.openWorldHint is False)

    print("\n[a summary of what is gated]")
    dest = sorted(n for n, t in tools.items() if t.annotations.destructiveHint)
    ro = sorted(n for n, t in tools.items() if t.annotations.readOnlyHint)
    print(f"  destructive ({len(dest)}): {', '.join(dest)}")
    print(f"  read-only   ({len(ro)}): {', '.join(ro)}")
    print(f"  other       ({len(tools) - len(dest) - len(ro)})")

    print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
    return 1 if fails else 0


sys.exit(main_())
