"""The shared static host — serves every app's frontend bundle.

One always-warm container fronts all frontends. It picks the bundle by the
request's Host header (`<app-id><DOMAIN_SUFFIX>` -> /srv/frontends/<app-id>) and
resolves each request **static-first**:

  1. a path the app explicitly declared as backend-owned  -> proxy to backend
  2. a file that exists in the bundle                     -> serve it
  3. a browser navigation (GET + Accept: text/html)       -> index.html
  4. anything else                                        -> proxy to backend

Step 3 is what makes SPA deep links work with no configuration, and it
deliberately does NOT consult the backend — so a hard navigation never wakes a
sleeping backend and never costs a cold start on first paint. Step 4 is where
XHR/fetch calls go, keeping their real status codes (an API 404 stays a 404
instead of being replaced by index.html).

Step 1 exists because the heuristic has real blind spots: an OAuth callback, a
download link, or a server-rendered page is a browser navigation that the backend
must answer. Declaring those prefixes is the escape hatch — not the default.

Backend proxying goes back through Traefik (Host: <app-id>.internal) rather than
straight to the container, so the Sablier middleware runs and a sleeping backend
is woken. Talking to the container directly would bypass it.
"""
import json
import os
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

ROOT = Path(os.environ.get("FRONTEND_ROOT", "/srv/frontends"))
DOMAIN_SUFFIX = os.environ.get("DOMAIN_SUFFIX", "-coolify.bogdanripa.com")
PROXY = os.environ.get("TRAEFIK_URL", "http://coolify-proxy")

# Hashed build output is safe to cache forever; everything else is not.
_HASHED = re.compile(r"[.-][0-9a-f]{8,}\.[a-z0-9]+$", re.I)
IMMUTABLE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"
NO_STORE = "no-store, private"

app = FastAPI(title="pironman-web", docs_url=None, redoc_url=None)


def _app_id(host: str) -> str | None:
    host = (host or "").split(":")[0].strip().lower()
    if not host.endswith(DOMAIN_SUFFIX):
        return None
    aid = host[: -len(DOMAIN_SUFFIX)]
    return aid if aid and "/" not in aid and "." not in aid else None


def _manifest(aid: str) -> dict:
    """Per-app config written by paas-api next to the bundle. Absent/broken means
    'static only, no backend', which is the safe reading."""
    try:
        return json.loads((ROOT / aid / ".pironman.json").read_text())
    except (OSError, ValueError):
        return {}


def _safe_file(aid: str, path: str) -> Path | None:
    """Resolve a URL path inside the app's bundle, refusing traversal."""
    base = (ROOT / aid).resolve()
    target = (base / path.lstrip("/")).resolve()
    if not str(target).startswith(str(base)):
        return None
    return target if target.is_file() else None


async def _proxy(request: Request, aid: str) -> Response:
    """Forward to the app's backend through Traefik so Sablier can wake it."""
    url = httpx.URL(PROXY).join(request.url.path).copy_with(query=request.url.query.encode())
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    headers["Host"] = f"{aid}.internal"
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as c:
            r = await c.request(request.method, url, headers=headers,
                                content=await request.body())
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"backend unreachable: {e}"}, status_code=502)

    out = dict(r.headers)
    for h in ("content-encoding", "content-length", "transfer-encoding", "connection"):
        out.pop(h, None)
    # Never let a proxied (potentially per-user) response be cached at the edge.
    out["cache-control"] = NO_STORE
    return Response(content=r.content, status_code=r.status_code, headers=out)


def _serve(f: Path, url_path: str) -> FileResponse:
    if f.name == "index.html":
        cc = NO_CACHE
    elif _HASHED.search(f.name) or url_path.startswith(("/assets/", "/static/")):
        cc = IMMUTABLE
    else:
        cc = "public, max-age=300"
    return FileResponse(f, headers={"Cache-Control": cc})


@app.get("/_health", include_in_schema=False)
async def health():
    return {"ok": True}


@app.api_route("/{_path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH",
                                         "DELETE", "OPTIONS"])
async def resolve(request: Request, _path: str = ""):
    aid = _app_id(request.headers.get("host", ""))
    if not aid or not (ROOT / aid).is_dir():
        return JSONResponse({"error": "no frontend for this host"}, status_code=404)

    mf = _manifest(aid)
    has_backend = bool(mf.get("has_backend"))
    path = request.url.path

    # 1. explicitly declared backend paths win outright
    for prefix in mf.get("backend_routes") or []:
        if prefix and (path == prefix or path.startswith(prefix.rstrip("/") + "/")
                       or prefix == "/"):
            return await _proxy(request, aid) if has_backend else JSONResponse(
                {"error": "no backend"}, status_code=404)

    # 2. a real file in the bundle
    if request.method in ("GET", "HEAD"):
        f = _safe_file(aid, path)
        if f:
            return _serve(f, path)

    # 3. browser navigation -> SPA entrypoint, without touching the backend
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        index = _safe_file(aid, "/index.html")
        if index:
            return _serve(index, "/index.html")

    # 4. everything else belongs to the backend
    if has_backend:
        return await _proxy(request, aid)
    return JSONResponse({"error": "not found"}, status_code=404)
