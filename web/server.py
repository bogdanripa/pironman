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

Step 5 needs the backend to have actually answered. A backend that could not be
reached is a 502, never a fallback — see _proxy.

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

Backend proxying goes back through Traefik rather than straight to the container,
so service discovery keeps working across redeploys (container names carry a
per-deploy timestamp). The request keeps its **real Host**: an app must not be
able to tell it is behind the static host, or every absolute URL it builds — OAuth
issuer metadata, WWW-Authenticate challenges, redirects — comes out wrong. The
backend's router is told apart from the frontend's by a marker header the static
host adds; see app/routing.py.

That marker also detects a backend that is asleep. Traefik's Docker provider only
sees running containers, so a stopped one has no router at all — the forwarded
request matches the frontend router instead and arrives back here. Rather than
mistaking that for a client-side route and serving index.html (which is what made
a dead backend look like a working site), the static host recognises its own
marker, wakes the app through Sablier's API and retries. The caller waits a few
seconds; nothing shows an interstitial.
"""
import asyncio
import json
import os
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)

import redirects as redirect_rules

ROOT = Path(os.environ.get("FRONTEND_ROOT", "/srv/frontends"))
DOMAIN_SUFFIX = os.environ.get("DOMAIN_SUFFIX", "-coolify.bogdanripa.com")
PROXY = os.environ.get("TRAEFIK_URL", "http://coolify-proxy")

# Kept in step with app/routing.py, which writes the matching Traefik rule.
BACKEND_HEADER = "x-pironman-backend"
BACKEND_TOKEN = "1"
# Marks the response this server returns when it receives its own forwarded
# request back — i.e. Traefik had no backend router to send it to.
DOWN_HEADER = "x-pironman-backend-down"

# This server's own app id. It hosts other apps' sites, never one of its own, so
# its hostname answers 404 — which is also why its healthcheck is on /_health. A
# bundle left in its directory (a stray test upload) must not change that.
SELF_APP_ID = os.environ.get("STATIC_HOST_APP", "web")

SABLIER_URL = os.environ.get("SABLIER_URL", "http://sablier:10000").rstrip("/")
SABLIER_SESSION_DURATION = os.environ.get("SABLIER_SESSION_DURATION", "5m")
WAKE_TIMEOUT = float(os.environ.get("WAKE_TIMEOUT_SECONDS", "60"))
# After Sablier reports the app ready, Traefik still has to notice the container
# and pass its healthcheck filter. A few short retries cover that gap.
WAKE_RETRIES = 8
WAKE_RETRY_DELAY = 1.5

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
        return json.loads((ROOT / aid / MANIFEST).read_text())
    except (OSError, ValueError):
        return {}


MANIFEST = ".pironman.json"


def _safe_file(aid: str, path: str) -> Path | None:
    """Resolve a URL path inside the app's bundle, refusing traversal.

    The manifest is platform config that happens to live next to the bundle, not
    part of the site — an app fronted only because it sleeps has nothing else in
    its directory at all. Serving it would publish our routing config on the app's
    own domain.
    """
    base = (ROOT / aid).resolve()
    target = (base / path.lstrip("/")).resolve()
    if not str(target).startswith(str(base)) or target.name == MANIFEST:
        return None
    return target if target.is_file() else None


async def _send(request: Request, aid: str):
    """One hop to the backend through Traefik. Returns (client, response) with the
    response still streaming, or None if the connection itself failed.

    The Host header is passed through untouched — the app must see the hostname
    the caller used. `content-length` is dropped because httpx recomputes it, and
    a stale one would contradict the body we send.
    """
    url = httpx.URL(PROXY).join(request.url.path).copy_with(
        query=request.url.query.encode())
    headers = {k: v for k, v in request.headers.items()
               if k.lower() != "content-length"}
    headers[BACKEND_HEADER] = BACKEND_TOKEN

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=None), follow_redirects=False)
    req = client.build_request(request.method, url, headers=headers,
                               content=await request.body())
    try:
        return client, await client.send(req, stream=True)
    except httpx.HTTPError:
        await client.aclose()
        return None


def _stream(client: httpx.AsyncClient, r: httpx.Response) -> StreamingResponse:
    """Hand the backend's response straight through as it arrives.

    Streaming rather than buffering: buffering looks fine for JSON and quietly
    breaks anything long-lived — an SSE endpoint (the MCP transport is one) never
    completes, so the caller waits for the timeout instead of receiving events,
    and a large download is held in memory before a byte reaches the client.
    """
    out = dict(r.headers)
    # Hop-by-hop headers describe the backend connection, not this one. Leaving
    # content-length in place would contradict a streamed body.
    for h in ("content-encoding", "content-length", "transfer-encoding", "connection"):
        out.pop(h, None)
    # Never let a proxied (potentially per-user) response be cached at the edge.
    out["cache-control"] = NO_STORE

    async def body():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=r.status_code, headers=out)


async def _wake(aid: str) -> bool:
    """Ask Sablier to start the app and block until it is ready.

    This is the piece Traefik cannot do for us: its Docker provider only sees
    running containers, so a stopped app has no router and its Sablier middleware
    is not reachable either. Calling Sablier directly closes that gap, and the
    blocking strategy means the caller just waits instead of being shown a page.

    Asked for by **group**, not by name: enrollment tags the container
    `sablier.group=<app-id>` (a stable id), while its actual name is the Coolify
    uuid plus a deploy timestamp and changes every release. `names=` is tried as a
    fallback only in case a container was enrolled by name by hand.
    """
    base = {"session_duration": SABLIER_SESSION_DURATION,
            "timeout": f"{int(WAKE_TIMEOUT)}s"}
    try:
        async with httpx.AsyncClient(timeout=WAKE_TIMEOUT + 10) as c:
            for key in ("group", "names"):
                r = await c.get(f"{SABLIER_URL}/api/strategies/blocking",
                                params={key: aid, **base})
                if r.status_code == 200:
                    return True
    except httpx.HTTPError:
        return False
    return False


def _is_down(r: httpx.Response) -> bool:
    """True when this is our own loop-guard answer coming back — Traefik had no
    backend router, so the request we forwarded returned to us."""
    return r.status_code == 503 and DOWN_HEADER in r.headers


async def _proxy(request: Request, aid: str) -> Response:
    """Forward to the app's backend, waking it first if it turns out to be asleep.

    A sleeping app is not an error to report: the request simply takes a few
    seconds. It is only when waking fails that this gives up, and then it says so
    plainly rather than falling through to the frontend — a dead backend that
    answers with the site's homepage is far harder to notice than a 502.
    """
    sent = await _send(request, aid)
    if sent and not _is_down(sent[1]):
        return _stream(*sent)

    if sent:
        await sent[1].aclose()
        await sent[0].aclose()
        if not await _wake(aid):
            return JSONResponse(
                {"error": "backend is not running and could not be started",
                 "app": aid}, status_code=502)

    # Either the connection failed outright or we have just woken the app; give
    # Traefik a moment to pick the container up, then try again.
    for _ in range(WAKE_RETRIES):
        await asyncio.sleep(WAKE_RETRY_DELAY)
        sent = await _send(request, aid)
        if sent and not _is_down(sent[1]):
            return _stream(*sent)
        if sent:
            await sent[1].aclose()
            await sent[0].aclose()

    return JSONResponse({"error": "backend unreachable", "app": aid},
                        status_code=502)


def _serve(f: Path, url_path: str) -> FileResponse:
    if f.name in INDEX_FILES:
        cc = NO_CACHE
    elif _HASHED.search(f.name) or url_path.startswith(("/assets/", "/static/")):
        cc = IMMUTABLE
    else:
        cc = "public, max-age=300"
    return FileResponse(f, headers={"Cache-Control": cc})


@app.get("/_health", include_in_schema=False)
async def health(request: Request):
    """This server's own liveness check, which Coolify calls on localhost.

    It only claims the path when the Host is not a hosted app: an app that
    happens to serve /_health itself must not have it silently answered by the
    platform instead.
    """
    aid = _app_id(request.headers.get("host", ""))
    if aid and (ROOT / aid).is_dir():
        return await resolve(request, "_health")
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

    # A request carrying our own marker was forwarded by this server and has come
    # straight back, which means Traefik matched the frontend router instead of
    # the backend's — the backend container is not running. Say exactly that,
    # with a header the forwarding side recognises, and stop: serving a file here
    # would hand the caller the site's homepage in place of the API's answer.
    if request.headers.get(BACKEND_HEADER) == BACKEND_TOKEN:
        return JSONResponse({"error": "backend has no route", "app": aid},
                            status_code=503, headers={DOWN_HEADER: "1"})

    if not aid or aid == SELF_APP_ID or not (ROOT / aid).is_dir():
        return JSONResponse({"error": "no frontend for this host"}, status_code=404)

    mf = _manifest(aid)
    has_backend = bool(mf.get("has_backend"))
    path = request.url.path

    # 0. Redirects come first, so a rule for a path still present in the bundle
    #    (or still served by the backend) actually takes effect — a redirect that
    #    silently loses to an existing file is the confusing case.
    hit = redirect_rules.match(mf.get("redirects") or [], path,
                               request.url.query or "")
    if hit:
        location, status = hit
        return RedirectResponse(location, status_code=status,
                                headers={"Cache-Control": NO_CACHE})

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
    #    json) keep the backend's real 404 instead of being handed HTML. Status
    #    is known as soon as the backend's headers arrive, so this costs nothing
    #    for a streamed response.
    if resp.status_code == 404 and "text/html" in request.headers.get("accept", ""):
        index = _index_of(aid)
        if index:
            return _serve(index, "/")
    return resp
