#!/usr/bin/env bash
# Run from /opt/paas — adds the MCP layer and clean tool names.
set -euo pipefail
cd "$(dirname "$0")"

python3 - << 'PYEOF'
import re

# --- 1. dependency ---
p = 'requirements.txt'; s = open(p).read()
if 'fastapi-mcp' not in s:
    open(p, 'w').write(s.rstrip() + "\nfastapi-mcp==0.4.0\n")

# --- 2. explicit operation_ids => clean MCP tool names, plus docstrings
#        (fastapi-mcp uses operation_id as the tool name and the summary/
#        docstring as the tool description, so these ARE the model's prompt) ---
ops = {
 'app/routers/apps.py': [
   ('@router.get("")',
    '@router.get("", operation_id="list_apps", summary="List all deployed apps")'),
   ('@router.get("/{app_id}")',
    '@router.get("/{app_id}", operation_id="get_app", summary="Get one app with its crons and database URL")'),
   ('@router.post("", status_code=201)',
    '@router.post("", status_code=201, operation_id="create_app", summary="Create and deploy a new app from a docker image, optionally with a postgres or mongo database")'),
   ('@router.put("/{app_id}/code")',
    '@router.put("/{app_id}/code", operation_id="update_app_code", summary="Point an app at a new image tag and redeploy it")'),
   ('@router.delete("/{app_id}", status_code=204)',
    '@router.delete("/{app_id}", status_code=204, operation_id="delete_app", summary="Delete an app, its database and its crons")'),
 ],
 'app/routers/query.py': [
   ('@router.post("/{app_id}/db/query")',
    '@router.post("/{app_id}/db/query", operation_id="run_db_script", summary="Run raw SQL or a mongo script against an app\'s own database")'),
 ],
 'app/routers/crons.py': [
   ('@router.get("/{app_id}/crons")',
    '@router.get("/{app_id}/crons", operation_id="list_crons", summary="List an app\'s scheduled HTTP jobs")'),
   ('@router.post("/{app_id}/crons", status_code=201)',
    '@router.post("/{app_id}/crons", status_code=201, operation_id="create_cron", summary="Schedule a recurring HTTP call to an app")'),
   ('@router.put("/{app_id}/crons/{cron_id}")',
    '@router.put("/{app_id}/crons/{cron_id}", operation_id="update_cron", summary="Change a scheduled job")'),
   ('@router.delete("/{app_id}/crons/{cron_id}", status_code=204)',
    '@router.delete("/{app_id}/crons/{cron_id}", status_code=204, operation_id="delete_cron", summary="Remove a scheduled job")'),
 ],
}
for path, pairs in ops.items():
    s = open(path).read()
    for old, new in pairs:
        if 'operation_id' not in s.split(old)[0][-200:] and old in s:
            s = s.replace(old, new, 1)
    open(path, 'w').write(s)

# --- 3. mount MCP ---
p = 'app/main.py'; s = open(p).read()
if 'FastApiMCP' not in s:
    s = s.replace(
      'from .routers import apps, crons, query',
      'from .routers import apps, crons, query')
    s = s.replace('''@app.get("/health", tags=["meta"])
async def health():
    return {"ok": True}''',
'''@app.get("/health", tags=["meta"], operation_id="health")
async def health():
    return {"ok": True}


# MCP: exposes the same endpoints as tools at /mcp. Mounted in-process, so it
# shares this app's auth — clients pass the same bearer key.
from fastapi_mcp import FastApiMCP  # noqa: E402

_mcp = FastApiMCP(
    app,
    name="pironman-paas",
    description=(
        "Deploy and manage apps on Bogdan's Raspberry Pi. An app is one arm64 "
        "docker container listening on port 80, reachable at "
        "https://<id>-coolify.bogdanripa.com, optionally with a postgres or "
        "mongo database and scheduled HTTP jobs."
    ),
)
_mcp.mount_http()''')
open(p, 'w').write(s)
print("patched")
PYEOF

echo "--- verify ---"
grep -n operation_id app/routers/*.py | head -20
grep -n "FastApiMCP\|mount_http" app/main.py
