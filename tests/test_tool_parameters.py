"""An optional parameter is a guessed parameter. Every default must be harmless.

Run it directly: `python tests/test_tool_parameters.py`.

OpenAI's strict tool calling fills EVERY property in a tool's schema, whether or
not the model has an opinion about it. So "optional" does not mean "the caller
will leave it alone" — it means "the caller will invent a value and the platform
will act on it". On 2026-09-24 that provisioned a Postgres database nobody asked
for, because `db_engine` was an optional enum and an agent filled it in.

Requiring the parameter does not stop the guessing. What it changes is WHICH
answer is obvious: a required enum whose first member is "none" gets guessed as
"none", and that costs nothing. So the rule this file enforces is not "make
everything required" — it is:

    every optional parameter's DEFAULT must be safe to apply without being
    asked, and every parameter whose value only the caller can know must be
    required.

The allowlist below is that review, written down. Adding a new optional
parameter fails this test until someone puts it in the list with a reason, which
is the point: the failure is a prompt to think, not a chore.
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


# tool -> {parameter: why its default is safe to guess}. Reviewed by hand; the
# test exists to make the next addition a deliberate act.
SAFE_OPTIONAL = {
    "apps_create":        {"health_path": "'/' is the only answer that is right "
                                          "for an app with no health endpoint, "
                                          "and a wrong one only delays the "
                                          "first deploy's verification",
                           "spa": "false serves a real 404 for an unmatched "
                                  "path, which is what a non-SPA wants",
                           "staging": "false creates one app, the status quo",
                           "github_repo": "absent means the key is returned "
                                          "instead of installed — no side "
                                          "effect either way",
                           "secret_name": "names the secret only when "
                                          "github_repo is given"},
    "apps_update":        {"auto_update": "null means leave unchanged",
                           "sleep_when_idle": "null means leave unchanged",
                           "spa": "null means leave unchanged",
                           "health_path": "null means leave unchanged",
                           "image": "null means leave unchanged"},
    "apps_deploy_key":    {"github_repo": "absent means return the key instead "
                                          "of installing it",
                           "secret_name": "names the secret only when "
                                          "github_repo is given"},
    "apps_deploy_workflow": {"repo_name": "defaults to the app id, which is the "
                                          "convention",
                             "dev_app": "absent means a single-app workflow",
                             "kind": "absent means detect it from the repo"},
    "crons_create":       {"method": "GET is the safe verb to guess; a guessed "
                                     "POST would submit something",
                           "body": "null sends no body",
                           "enabled": "true is the point of creating a schedule"},
    "crons_update":       {"schedule": "null means leave unchanged",
                           "method": "null means leave unchanged",
                           "path": "null means leave unchanged",
                           "body": "null means leave unchanged",
                           "enabled": "null means leave unchanged"},
    "env_set":            {"redeploy": "true is what makes the variable reach "
                                       "the container; false only delays it"},
    "apps_env_set":       {"redeploy": "as env_set"},
    "env_delete":         {"redeploy": "as env_set"},
    "apps_env_delete":    {"redeploy": "as env_set"},
    "host_run_script":    {"timeout": "a wrong timeout kills the script early; "
                                      "it cannot make it do more"},
    "db_run_script":      {"timeout": "as host_run_script"},
    "db_read_query":      {"timeout": "a wrong timeout ends the read early; "
                                      "the connection cannot write either way"},
    "apps_logs":          {"lines": "a read, bounded either way",
                           "since": "a read, bounded either way"},
    "apps_stats":         {"app_id": "absent means every app — a read"},
    "apps_get":           {"reveal_db_password": "false masks the password, "
                                                 "which is the safe direction"},
    "deploys_status":     {"app_id": "absent means every app — a read",
                           "limit": "a read, bounded either way"},
}

# Parameters that must be REQUIRED: only the caller knows them, and a guess
# costs money, data or a promise that is hard to take back.
MUST_BE_REQUIRED = {
    "apps_create": ["id", "db_engine"],
    "crons_create": ["schedule", "path"],
    "apps_redirects_set": [],     # the rules list itself is required
    "apps_attach_db": ["db_engine"],
}


def main_():
    tools = {t.name: t for t in (getattr(main._mcp, "tools", None) or [])}
    check("tools are exposed", len(tools) > 20, f"{len(tools)}")

    print("\n[parameters that must not be guessable]")
    for tool, params in MUST_BE_REQUIRED.items():
        if tool not in tools:
            continue
        schema = tools[tool].inputSchema or {}
        required = set(schema.get("required") or [])
        props = set((schema.get("properties") or {}).keys())
        for prm in params:
            if prm not in props:
                check(f"{tool}.{prm} exists", False, f"properties: {sorted(props)}")
                continue
            check(f"{tool}.{prm} is required", prm in required,
                  f"required: {sorted(required)}")

    print("\n[db_engine offers an explicit, free 'none']")
    if "apps_create" in tools:
        props = (tools["apps_create"].inputSchema or {}).get("properties") or {}
        enum = props.get("db_engine", {}).get("enum") or []
        # pydantic may nest it under anyOf/$ref; fall back to the raw text.
        raw = str(props.get("db_engine", {}))
        check("'none' is one of the choices", "none" in enum or "'none'" in raw, raw[:160])
        check("'postgres' is still a choice", "postgres" in enum or "postgres" in raw)

    print("\n[every optional parameter on a WRITING tool has been reviewed]")
    # A read-only tool's parameters can only change what comes back, so a
    # guessed value costs a wrong answer, never a side effect. They are exempt
    # by construction rather than by a list nobody would keep current — with one
    # exception below, because "read-only" and "harmless" part company the
    # moment a read can disclose a secret.
    unreviewed = []
    for name, tool in sorted(tools.items()):
        if (tool.annotations is not None and tool.annotations.readOnlyHint):
            continue
        schema = tool.inputSchema or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        for prm in props:
            if prm in required:
                continue
            if prm in ("authorization",):   # injected by the auth dependency
                continue
            if prm in SAFE_OPTIONAL.get(name, {}):
                continue
            unreviewed.append(f"{name}.{prm}")
    check("no unreviewed optional parameter on a writing tool", not unreviewed,
          "\n         add to SAFE_OPTIONAL with a reason, or make it required: "
          + ", ".join(unreviewed) if unreviewed else "")

    print("\n[a read that can disclose a secret must default to not disclosing]")
    # The exemption above would wave these through, and they are precisely the
    # ones a strict caller filling every field would flip to true.
    for tool_name, prm in (("apps_get", "reveal_db_password"),):
        if tool_name not in tools:
            continue
        props = (tools[tool_name].inputSchema or {}).get("properties") or {}
        if prm not in props:
            check(f"{tool_name}.{prm} exists", False, "not in the schema")
            continue
        default = props[prm].get("default")
        check(f"{tool_name}.{prm} defaults to withholding", default is False,
              f"default={default!r}")

    print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
    return 1 if fails else 0


sys.exit(main_())
