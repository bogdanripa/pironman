"""End-to-end exercise of server.py against a fake Traefik + fake Sablier.

Run it directly: `python web/tests/test_server.py` (needs fastapi, httpx, uvicorn).

Simulates the real topology:

    client -> [fake Traefik] -> static host -> [fake Traefik] -> backend

The fake Traefik implements the two routers the platform writes: a backend router
that matches only when the marker header is present AND the container is running,
and a frontend router (Host only) that points at the static host. So when the
backend is "asleep" the forwarded request comes back to the static host, exactly
as it does on the box.
"""
import asyncio, json, os, socket, sys, tempfile, threading
from pathlib import Path

ROOT = Path(tempfile.mkdtemp())
(ROOT / "demo").mkdir()
(ROOT / "demo" / "index.html").write_text("<h1>MARKETING PAGE</h1>")
(ROOT / "demo" / ".pironman.json").write_text(json.dumps(
    {"has_backend": True, "redirects": []}))
# A stray bundle in the static host's own directory: it must still 404 its own
# hostname rather than serve whatever was left there.
(ROOT / "web").mkdir()
(ROOT / "web" / "index.html").write_text("<h1>SHOULD NEVER BE SERVED</h1>")
(ROOT / "static-only").mkdir()
(ROOT / "static-only" / "index.html").write_text("<h1>STATIC</h1>")
(ROOT / "static-only" / "404.html").write_text("<h1>CUSTOM 404</h1>")
(ROOT / "static-only" / ".pironman.json").write_text(json.dumps(
    {"has_backend": False, "redirects": []}))
# The same site with client-side routing turned on.
(ROOT / "spa-app").mkdir()
(ROOT / "spa-app" / "index.html").write_text("<h1>SPA SHELL</h1>")
(ROOT / "spa-app" / ".pironman.json").write_text(json.dumps(
    {"has_backend": False, "redirects": [], "spa": True}))


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


WEB_PORT, PROXY_PORT, SABLIER_PORT = free_port(), free_port(), free_port()

os.environ["FRONTEND_ROOT"] = str(ROOT)
os.environ["DOMAIN_SUFFIX"] = "-test.local"
os.environ["TRAEFIK_URL"] = f"http://127.0.0.1:{PROXY_PORT}"
os.environ["SABLIER_URL"] = f"http://127.0.0.1:{SABLIER_PORT}"
os.environ["WAKE_TIMEOUT_SECONDS"] = "5"

# server.py lives one level up; the image copies only *.py from web/, so
# keeping the test in a subdirectory keeps it out of the container.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402
server.WAKE_RETRY_DELAY = 0.2
# The budget is wall-clock, so cap it here: the retry path is exercised by the
# dead-backend cases, which would otherwise burn the full production budget
# failing before the assertion runs.
server.WAKE_RETRY_BUDGET = 1.0
server.GATEWAY_RETRY_BUDGET = 1.0

import httpx, uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, PlainTextResponse  # noqa: E402

STATE = {"up": False, "wakes": 0, "sablier_ok": True, "seen_host": None,
         "seen_xfh": None, "asked_by": None, "gateway": 0}

proxy = FastAPI()


@proxy.api_route("/{p:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD"])
async def route(request: Request, p: str = ""):
    """The two Traefik routers, in priority order."""
    marker = request.headers.get(server.BACKEND_HEADER) == server.BACKEND_TOKEN
    # Traefik answering 502 on its own account: it HAS a router for the backend
    # but no healthy container behind it. Carries none of our headers, which is
    # exactly why it used to be forwarded to the caller as if the app had said
    # it. `gateway` counts down so a test can make the window transient, the way
    # the real one is on a deploy.
    if marker and STATE["gateway"] > 0:
        STATE["gateway"] -= 1
        return PlainTextResponse("Bad Gateway", status_code=502)
    if marker and STATE["up"]:
        # backend router matched -> the app itself
        STATE["seen_host"] = request.headers.get("host")
        STATE["seen_xfh"] = request.headers.get("x-forwarded-host")
        if request.url.path == "/sse":
            from fastapi.responses import StreamingResponse
            async def gen():
                for i in range(3):
                    yield f"data: {i}\n\n".encode()
                    await asyncio.sleep(0.4)
            return StreamingResponse(gen(), media_type="text/event-stream")
        if request.url.path == "/gzipped":
            # What Traefik's gzip middleware does to any response over ~1KB.
            import gzip as _gzip
            from fastapi.responses import Response as _Resp
            page = ("<html><body>" + "CONSENT PAGE " * 200 + "</body></html>").encode()
            return _Resp(_gzip.compress(page), media_type="text/html",
                         headers={"content-encoding": "gzip"})
        if request.url.path.startswith("/api/"):
            return JSONResponse({"backend": True, "path": request.url.path})
        return JSONResponse({"error": "not_found"}, status_code=404)
    # frontend router matched -> back to the static host
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(
            request.method, f"http://127.0.0.1:{WEB_PORT}{request.url.path}",
            headers={k: v for k, v in request.headers.items()},
            content=await request.body())
    return PlainTextResponse(r.text, status_code=r.status_code,
                             headers={k: v for k, v in r.headers.items()
                                      if k.lower() not in ("content-length",
                                                           "content-encoding")})


sablier = FastAPI()


@sablier.get("/api/strategies/blocking")
async def blocking(group: str = "", names: str = "", session_duration: str = "",
                   timeout: str = ""):
    """Real Sablier answers by group OR by name. Ours enrols by group — the
    container's name carries a deploy timestamp — so a wake asked for by name
    finds nothing, which is a 500 in practice."""
    STATE["wakes"] += 1
    STATE["asked_by"] = "group" if group else "names" if names else None
    if not group:
        return JSONResponse({"error": "no such instance"}, status_code=500)
    if not STATE["sablier_ok"]:
        return JSONResponse({"error": "nope"}, status_code=500)
    STATE["up"] = True
    return {"instances": [{"name": group, "status": "ready"}]}


def serve(app, port):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    threading.Thread(target=uvicorn.Server(cfg).run, daemon=True).start()


serve(server.app, WEB_PORT)
serve(proxy, PROXY_PORT)
serve(sablier, SABLIER_PORT)


async def main():
    await asyncio.sleep(1.2)
    base = f"http://127.0.0.1:{PROXY_PORT}"          # enter through "Traefik"
    H = {"Host": "demo-test.local"}
    fails = []

    def check(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    async with httpx.AsyncClient(timeout=60) as c:
        print("\n[backend running]")
        STATE["up"] = True
        r = await c.get(base + "/", headers=H)
        check("GET / serves the bundle", "MARKETING PAGE" in r.text, r.status_code)
        r = await c.get(base + "/api/thing", headers=H)
        check("GET /api/* reaches the backend",
              r.status_code == 200 and r.json().get("backend"), r.text[:60])
        check("backend sees the REAL public host",
              STATE["seen_host"] == "demo-test.local", STATE["seen_host"])
        r = await c.get(base + "/deep/link", headers={**H, "Accept": "text/html"})
        check("a browser deep link is a 404, not the homepage",
              r.status_code == 404 and "MARKETING PAGE" not in r.text,
              f"{r.status_code} {r.text[:40]}")
        r = await c.get(base + "/deep/link", headers={**H, "Accept": "application/json"})
        check("json client keeps the backend's 404", r.status_code == 404, r.text[:60])
        r = await c.get(base + "/.pironman.json", headers=H)
        check("manifest is never served", "has_backend" not in r.text, r.text[:60])

        print("\n[backend asleep -> wakes]")
        STATE["up"] = False; STATE["wakes"] = 0
        r = await c.get(base + "/api/thing", headers=H)
        check("asleep backend is woken and the request succeeds",
              r.status_code == 200 and r.json().get("backend"), r.text[:80])
        check("Sablier was called exactly once", STATE["wakes"] == 1, STATE["wakes"])
        check("the wake asks by group, not container name",
              STATE["asked_by"] == "group", STATE["asked_by"])

        print("\n[backend asleep, browser navigation]")
        STATE["up"] = False; STATE["wakes"] = 0
        r = await c.get(base + "/api/thing", headers={**H, "Accept": "text/html"})
        check("navigation to an API path still wakes the backend",
              r.status_code == 200 and "MARKETING" not in r.text, r.text[:60])

        print("\n[Traefik answers 502 while the backend is coming up]")
        # The real bug: for a second or two after a deploy Traefik has the
        # router but no healthy server, and answers 502 itself. It carries none
        # of our headers, so it used to be streamed to the caller verbatim with
        # no wake attempted — a request the platform could have served, failed.
        STATE["up"] = True; STATE["gateway"] = 2; STATE["wakes"] = 0
        r = await c.get(base + "/api/thing", headers=H)
        check("a transient gateway 502 is retried, not handed to the caller",
              r.status_code == 200 and r.json().get("backend"), r.text[:80])

        print("\n[the app itself keeps answering 502]")
        # The other side of the same coin: if it really is the app's error, the
        # app's error is what the caller must get -- not an invented one of ours.
        STATE["up"] = True; STATE["gateway"] = 9999; STATE["wakes"] = 0
        r = await c.get(base + "/api/thing", headers=H)
        check("a persistent 502 passes the backend's own response through",
              r.status_code == 502 and "Bad Gateway" in r.text, r.text[:60])
        STATE["gateway"] = 0

        print("\n[wake fails]")
        STATE["up"] = False; STATE["sablier_ok"] = False; STATE["wakes"] = 0
        r = await c.get(base + "/api/thing", headers={**H, "Accept": "text/html"})
        check("unreachable backend is a 502, NOT the homepage",
              r.status_code == 502 and "MARKETING" not in r.text,
              f"{r.status_code} {r.text[:60]}")
        r = await c.post(base + "/api/write", headers=H, json={"a": 1})
        check("write to a dead backend is a 502 too",
              r.status_code == 502, r.status_code)
        STATE["sablier_ok"] = True

        print("\n[a compressed backend response]")
        STATE["up"] = True
        r = await c.get(f"http://127.0.0.1:{WEB_PORT}/gzipped",
                        headers={**H, "Accept-Encoding": "gzip"})
        check("a gzipped page decodes rather than arriving as mojibake",
              "CONSENT PAGE" in r.text, r.text[:60])
        check("content-encoding is passed through, not silently dropped",
              r.headers.get("content-encoding") == "gzip",
              r.headers.get("content-encoding"))

        print("\n[SSE streaming, as the MCP transport uses]")
        STATE["up"] = True
        import time
        t0 = time.monotonic(); marks = []
        # straight at the static host: the outer fake-Traefik hop buffers with
        # r.text, which would mask streaming that is actually happening.
        async with c.stream("GET", f"http://127.0.0.1:{WEB_PORT}/sse", headers=H) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    marks.append(time.monotonic() - t0)
        check("all SSE events arrive", len(marks) == 3, marks)
        check("events stream rather than arriving in one buffered blob",
              len(marks) == 3 and marks[-1] - marks[0] > 0.5,
              [round(m, 2) for m in marks])

        print("\n[the static host's own hostname]")
        r = await c.get(base + "/", headers={"Host": "web-test.local"})
        check("serves no site of its own, even with a bundle present",
              r.status_code == 404 and "NEVER" not in r.text,
              f"{r.status_code} {r.text[:60]}")
        r = await c.get(f"http://127.0.0.1:{WEB_PORT}/_health")
        check("its own healthcheck still answers", r.status_code == 200, r.text[:40])

        print("\n[frontend-only app: a missing path is a 404]")
        H2 = {"Host": "static-only-test.local"}
        r = await c.get(base + "/anything", headers={**H2, "Accept": "text/html"})
        check("an unknown path serves the app's 404.html, with a 404 status",
              r.status_code == 404 and "CUSTOM 404" in r.text,
              f"{r.status_code} {r.text[:40]}")
        check("it is not the homepage", "STATIC" not in r.text)
        r = await c.get(base + "/anything", headers={**H2, "Accept": "application/json"})
        check("a json client gets a plain 404, not a web page",
              r.status_code == 404 and "CUSTOM 404" not in r.text, r.text[:60])
        r = await c.get(base + "/", headers=H2)
        check("the homepage itself still serves", "STATIC" in r.text)
        r = await c.post(base + "/anything", headers=H2, json={})
        check("write with no backend is 404", r.status_code == 404, r.status_code)

        print("\n[spa=true: unmatched paths are client-side routes]")
        H3 = {"Host": "spa-app-test.local"}
        r = await c.get(base + "/deep/route", headers={**H3, "Accept": "text/html"})
        check("a deep link serves the shell with a 200",
              r.status_code == 200 and "SPA SHELL" in r.text,
              f"{r.status_code} {r.text[:40]}")

        print("\n[spa=true with a backend: the shell wins over the backend 404]")
        STATE["up"] = True
        r = await c.get(base + "/deep/route", headers={**H, "Accept": "text/html"})
        check("without spa, the backend's own 404 stands",
              r.status_code == 404 and "MARKETING" not in r.text,
              f"{r.status_code} {r.text[:40]}")

    print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
