"""The shared static host — serves every app's frontend bundle.

One always-warm container fronts all frontends. It picks the bundle by the
request's Host header (`<app-id><DOMAIN_SUFFIX>` -> /srv/frontends/<app-id>) and
resolves each request with one rule: **a bundle only answers reads, and only for
files it actually has.**

  1. not GET/HEAD                    -> backend (a bundle cannot answer writes)
  2. a file in the bundle            -> serve it ("/" resolves to index.html)
  3. not a file, and no backend      -> index.html (it is a client-side route)
  4. not a file, backend exists      -> backend, and its answer stands
  5. backend said 404 + navigation   -> index.html after all

Nothing is declared anywhere. An OAuth callback, a download link and a
server-rendered page are all just GETs that are not files, so they reach the
backend like any other request. An earlier design intercepted those as
"navigations", served index.html, and then needed a per-app list of exceptions to
undo itself — the exceptions existed only because the heuristic created them.

Step 5 is the one heuristic left, and it is bounded: it applies solely to a
request the backend has already rejected, and only when the caller is a browser
navigating. Anything that wanted data keeps the backend's real 404 rather than
being handed HTML. The cost is one round trip to the backend before a client-side
deep link falls back — paid only on paths the backend does not serve, and only by
apps that have a backend at all.

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
# Entry files, in preference order. index.htm is the legacy spelling; supporting
# it costs nothing and avoids a site that looks empty for a puzzling reason.
INDEX_FILES = ("index.html", "index.htm")

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
    if f.name in INDEX_FILES:
        cc = NO_CACHE
    elif _HASHED.search(f.name) or url_path.startswith(("/assets/", "/static/")):
        cc = IMMUTABLE
    else:
        cc = "public, max-age=300"
    return FileResponse(f, headers={"Cache-Control": cc})


@app.get("/_health", include_in_schema=False)
async def health():
    return {"ok": True}


def _index_of(aid: str):
    for name in INDEX_FILES:
        f = _safe_file(aid, "/" + name)
        if f:
            return f
    return None


@app.api_route("/{_path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH",
                                         "DELETE", "OPTIONS"])
async def resolve(request: Request, _path: str = ""):
    aid = _app_id(request.headers.get("host", ""))
    if not aid or not (ROOT / aid).is_dir():
        return JSONResponse({"error": "no frontend for this host"}, status_code=404)

    has_backend = bool(_manifest(aid).get("has_backend"))
    path = request.url.path

    # 1. Anything that isn't a read belongs to the backend. A bundle only ever
    #    answers GET/HEAD, so there is nothing to check first.
    if request.method not in ("GET", "HEAD"):
        return await _proxy(request, aid) if has_backend else JSONResponse(
            {"error": "no backend"}, status_code=404)

    # 2. A real file in the bundle wins. A directory resolves to its index, the
    #    way any web server serves "/".
    f = _safe_file(aid, path)
    if f is None and path.endswith("/"):
        f = next((x for x in (_safe_file(aid, path + n) for n in INDEX_FILES)
                  if x), None)
    if f:
        return _serve(f, path)

    # 3. Not a file. With no backend there is nobody else to ask, so this is a
    #    client-side route: serve the SPA entrypoint.
    if not has_backend:
        index = _index_of(aid)
        return _serve(index, "/") if index else JSONResponse(
            {"error": "not found"}, status_code=404)

    # 4. There is a backend, so it gets the request — and its answer stands.
    #    This is what makes OAuth callbacks, download links and server-rendered
    #    pages work with nothing declared anywhere: they are simply GETs that are
    #    not files, so they reach the backend like everything else.
    resp = await _proxy(request, aid)

    # 5. Only if the backend also says "no such thing", and the caller is a
    #    browser navigating, is this a client-side route after all — serve the
    #    entrypoint. Requests that wanted data (fetch/XHR, Accept: application/
    #    json) keep the backend's real 404 instead of being handed HTML.
    if resp.status_code == 404 and "text/html" in request.headers.get("accept", ""):
        index = _index_of(aid)
        if index:
            return _serve(index, "/")
    return resp
