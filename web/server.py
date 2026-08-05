"""The shared static host — serves every app's frontend bundle.

One always-warm container fronts all frontends. It picks the bundle by the
request's Host header (`<app-id><DOMAIN_SUFFIX>` -> /srv/frontends/<app-id>) and
resolves each request with one rule: **a bundle only answers reads, and only for
files it actually has.**

  1. not GET/HEAD                    -> backend (a bundle cannot answer writes)
  2. a file in the bundle            -> serve it ("/" resolves to index.html)
  3. not a file, and no backend      -> nothing has it: see _not_found
  4. not a file, backend exists      -> backend, and its answer stands
  5. backend said 404                -> nothing has it: see _not_found

**A path nobody has is a 404.** If the bundle ships `404.html` that page is
served, with a 404 status. An app whose client-side router owns those paths opts
in (`spa` in the manifest) and gets its entrypoint instead — which is the older
behaviour, and was the default until it became clear what it costs: every typo
answering 200 with the homepage, no app able to show its own 404 page, and a
broken link indistinguishable from a working one.

Step 5 needs the backend to have actually answered. A backend that could not be
reached is a 502, never a fallback — see _proxy.

Nothing is declared anywhere. An OAuth callback, a download link and a
server-rendered page are all just GETs that are not files, so they reach the
backend like any other request. An earlier design intercepted those as
"navigations", served index.html, and then needed a per-app list of exceptions to
undo itself — the exceptions existed only because the heuristic created them.

What remains after step 5 is bounded by the same principle: only a request the
backend has already rejected is reconsidered, and only a caller rendering a page
can be given one. Anything that wanted data keeps the backend's real 404 rather
than being handed HTML.

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
import logging
import os
import re
import time
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
# and pass its healthcheck filter. Short retries cover that gap.
#
# FLAT cadence and a deadline, NOT a doubling backoff — and the difference is
# most of a cold wake. What we are waiting on is an edge: the instant Docker
# marks the container healthy and Traefik starts routing to it. That edge lands
# at an arbitrary moment, so the penalty for missing it is the whole of the next
# gap, and a schedule whose gaps grow is guaranteed to be at its worst exactly
# when the edge is latest.
#
# Measured on this box, 2026-08-05, against the doubling schedule (attempts at
# 0.35/0.55/0.95/1.75/3.35/5.35/7.35/9.35s): the app answered on its own
# container IP at 0.76s and Docker marked it healthy at 5.39s — and the 5.35s
# attempt missed that by 40ms, so nothing succeeded until 9.35s. Every cold wake
# measured 10.25-10.41s, and over half of it was spent asleep in here. The
# constancy was the tell: an accumulation varies, a missed slot does not.
#
# A flat 0.25s costs a handful of extra 503s against Traefik — which answers
# them immediately, having no route — and returns within a quarter second of
# whenever readiness actually happens. The budget is a wall-clock deadline
# rather than an attempt count so that tuning the cadence cannot silently change
# how long a genuinely dead backend takes to report itself.
WAKE_RETRY_DELAY = float(os.environ.get("WAKE_RETRY_DELAY_SECONDS", "0.25"))
WAKE_RETRY_BUDGET = float(os.environ.get("WAKE_RETRY_BUDGET_SECONDS", "15"))
# Shorter, for a bare gateway error where "asleep" is a guess rather than a
# marker we recognise. Long enough to cover a wake several times over now that
# Sablier answers in ~0.5s, short enough that an app which genuinely returns 502
# is not held for the full sleeping-app budget before its own error is shown.
GATEWAY_RETRY_BUDGET = float(os.environ.get("GATEWAY_RETRY_BUDGET_SECONDS", "5"))

# uvicorn already configures this one and it goes to stdout, so `apps_logs web`
# shows it without a logging setup of our own.
_log = logging.getLogger("uvicorn.error")

# Hashed build output is safe to cache forever; everything else is not.
# Entry files, in preference order. index.htm is the legacy spelling; supporting
# it costs nothing and avoids a site that looks empty for a puzzling reason.
INDEX_FILES = ("index.html", "index.htm")

# A bundle can ship its own not-found page. Conventional name, no configuration:
# if it is there it is used, and it is served with a 404 status rather than the
# 200 a soft-404 would give.
NOT_FOUND_FILE = "404.html"

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


MANIFEST = ".pironman.json"


def _manifest(aid: str) -> dict:
    """Per-app config written by paas-api next to the bundle. Absent/broken means
    'static only, no backend', which is the safe reading."""
    try:
        return json.loads((ROOT / aid / MANIFEST).read_text())
    except (OSError, ValueError):
        return {}


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
    # httpx would otherwise advertise gzip on its own behalf, and the response is
    # passed through still encoded — so a caller that never asked for compression
    # would be handed a compressed body. Ask for exactly what the caller asked for.
    headers.setdefault("accept-encoding", "identity")

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
    # Hop-by-hop headers describe the backend connection, not this one, and
    # content-length would contradict a streamed body.
    #
    # content-encoding is deliberately NOT dropped. The body is forwarded raw,
    # exactly as it arrived, and Traefik's gzip middleware sits on the backend's
    # router — so removing the header while still sending compressed bytes tells
    # the browser to render a gzip stream as text. That is what an OAuth consent
    # page looked like: a screenful of mojibake. Small responses were unaffected
    # (Traefik only compresses above ~1KB), which is why JSON endpoints and
    # healthchecks looked fine.
    for h in ("content-length", "transfer-encoding", "connection"):
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

    response = StreamingResponse(body(), status_code=r.status_code, headers=out)
    # So a caller that decides not to use this response can still close what is
    # behind it — see _release.
    response.upstream = (client, r)
    return response


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


# Traefik's own answer when it HAS a router for the backend but cannot reach a
# healthy container behind it. Not the same shape as _is_down: there the request
# came back to us carrying our marker, here Traefik answers on its own account
# and the reply is indistinguishable from the app's, because it is a plain HTTP
# error with none of our headers on it.
_GATEWAY_ERRORS = frozenset({502, 503, 504})


def _is_gateway_error(r: httpx.Response) -> bool:
    """True when the proxy — not the app — is reporting it could not reach the
    backend.

    This is the gap that let a wakeable app answer a caller with a 502. The
    window exists on every deploy: for a second or two Traefik has the router
    but no healthy server behind it, and it answers 502 itself. That is not our
    marker, so the old check waved it through as if the app had said it, the
    fast path returned it verbatim, and no wake was ever attempted. Measured on
    the box: 502 after 20.0s and 24.6s on the first request following a deploy,
    with a plain retry seconds later succeeding — the platform could have served
    every one of them.

    Deliberately excludes anything carrying DOWN_HEADER, which _is_down already
    owns. A response the app itself generated with one of these codes is
    retried too and that is accepted: 502/503/504 all mean the request was not
    fulfilled, so re-sending cannot double-apply it, and if the app really is
    broken its own error still reaches the caller — see the final send in
    _proxy, which passes through whatever comes back once the budget is spent.
    """
    return r.status_code in _GATEWAY_ERRORS and DOWN_HEADER not in r.headers


async def _proxy(request: Request, aid: str) -> Response:
    """Forward to the app's backend, waking it first if it turns out to be asleep.

    A sleeping app is not an error to report: the request simply takes a few
    seconds. It is only when waking fails that this gives up, and then it says so
    plainly rather than falling through to the frontend — a dead backend that
    answers with the site's homepage is far harder to notice than a 502.

    **The slow path logs its own breakdown**, because four separate attempts to
    explain a ten-second cold wake from the outside — timing the healthcheck,
    the image, the retry schedule, Sablier's refresh — each produced a confident
    story that the next measurement killed. Every one of them was inferring the
    inside of this function from the outside of it. The stage timings are cheap,
    they only fire when an app was actually asleep, and they answer the question
    directly instead of supporting another guess.
    """
    t0 = time.monotonic()
    sent = await _send(request, aid)
    if sent and not _is_down(sent[1]) and not _is_gateway_error(sent[1]):
        return _stream(*sent)          # fast path: awake, nothing logged
    probe = time.monotonic() - t0
    # A bare gateway error might be a sleeping app OR an app that is genuinely
    # answering 502, and we cannot tell the two apart from the response. So give
    # it the shorter budget: a real wake resolves in well under a second once
    # Sablier returns, while a broken app should surface its own error promptly
    # rather than being retried for the full sleeping-app budget.
    budget = WAKE_RETRY_BUDGET if (sent and _is_down(sent[1])) \
        else GATEWAY_RETRY_BUDGET

    sablier = 0.0
    if sent:
        await sent[1].aclose()
        await sent[0].aclose()
        t_wake = time.monotonic()
        woke = await _wake(aid)
        sablier = time.monotonic() - t_wake
        if not woke:
            _log.warning("wake %s: FAILED — probe %.2fs, sablier %.2fs, total "
                         "%.2fs", aid, probe, sablier, time.monotonic() - t0)
            return JSONResponse(
                {"error": "backend is not running and could not be started",
                 "app": aid}, status_code=502)

    # Either the connection failed outright or we have just woken the app; give
    # Traefik a moment to pick the container up, then try again — polling at a
    # flat cadence until the budget runs out, so we return as soon after
    # readiness as the cadence allows rather than at the next widening gap.
    deadline = time.monotonic() + budget
    tries = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(WAKE_RETRY_DELAY)
        tries += 1
        sent = await _send(request, aid)
        if sent and not _is_down(sent[1]) and not _is_gateway_error(sent[1]):
            _log.info("wake %s: served in %.2fs — probe %.2fs, sablier %.2fs, "
                      "then %d retr%s over %.2fs", aid, time.monotonic() - t0,
                      probe, sablier, tries, "y" if tries == 1 else "ies",
                      time.monotonic() - t0 - probe - sablier)
            return _stream(*sent)
        if sent:
            await sent[1].aclose()
            await sent[0].aclose()

    # Budget spent. Ask once more and pass through whatever comes back, so an
    # app that really is answering 502 surfaces ITS error and not an invented
    # one of ours — the failure should read as the app's, because it is. Only a
    # backend we cannot reach at all falls through to our own 502.
    sent = await _send(request, aid)
    if sent:
        _log.warning("wake %s: still failing after %.2fs — probe %.2fs, sablier "
                     "%.2fs, %d retries; passing the backend's %s through", aid,
                     time.monotonic() - t0, probe, sablier, tries,
                     sent[1].status_code)
        return _stream(*sent)

    _log.warning("wake %s: UNREACHABLE after %.2fs — probe %.2fs, sablier "
                 "%.2fs, %d retries", aid, time.monotonic() - t0, probe,
                 sablier, tries)
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


async def _release(resp: Response) -> None:
    """Close the upstream connection behind a proxied response we are discarding.

    A StreamingResponse only closes its upstream when its body is iterated, and
    a response we replace never is — so dropping one silently holds an httpx
    connection open until garbage collection.
    """
    upstream = getattr(resp, "upstream", None)
    if upstream:
        client, r = upstream
        await r.aclose()
        await client.aclose()


async def _not_found(aid: str, mf: dict, request: Request,
                     backend: Response | None = None) -> Response:
    """Answer a path that neither the bundle nor the backend has.

    In order:
      1. the app is a **single-page app** — the path belongs to its client-side
         router, so serve the entrypoint (200: the page really is the app);
      2. the bundle ships **404.html** and the caller is rendering a page — serve
         it, with a 404 status, so the app owns how "not found" looks;
      3. otherwise the backend's own 404 if it answered one, else a plain 404.

    Step 2 is gated on the caller wanting HTML because an API client asking for
    JSON is better served by the backend's real error than by a web page.
    """
    if mf.get("spa"):
        index = _index_of(aid)
        if index:
            if backend is not None:
                await _release(backend)
            return _serve(index, "/")

    if "text/html" in request.headers.get("accept", ""):
        page = _safe_file(aid, "/" + NOT_FOUND_FILE)
        if page:
            if backend is not None:
                await _release(backend)
            return FileResponse(page, status_code=404,
                                headers={"Cache-Control": NO_CACHE})

    if backend is not None:
        return backend
    return JSONResponse({"error": "not found"}, status_code=404)


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

    # 3. Not a file. With no backend there is nobody else to ask, so nothing has
    #    this path — unless the app says its router owns it.
    if not has_backend:
        return await _not_found(aid, mf, request)

    # 4. There is a backend, so it gets the request — and its answer stands.
    #    This is what makes OAuth callbacks, download links and server-rendered
    #    pages work with nothing declared anywhere: they are simply GETs that are
    #    not files, so they reach the backend like everything else.
    resp = await _proxy(request, aid)

    # 5. The backend has also said "no such thing", so nothing has this path.
    #    Its own 404 is the default answer and is what an API client keeps; the
    #    bundle only gets to speak for a page. Status is known as soon as the
    #    backend's headers arrive, so this costs nothing for a streamed response.
    if resp.status_code == 404:
        return await _not_found(aid, mf, request, backend=resp)
    return resp
