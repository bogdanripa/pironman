"""Check the Traefik rules app/routing.py generates against a REAL Traefik.

    python tests/test_traefik_rules.py              # both halves here
    python tests/test_traefik_rules.py --emit-rules # just print the rules as JSON
    python tests/test_traefik_rules.py --rules '<json>'   # check rules from stdin/arg

The split exists because the two halves need different things: generating the
rules needs the `app` package and its dependencies, while checking them needs a
docker daemon, and on this platform those live in different places — the control
plane's container has the code, the Pi's host has docker. Run `--emit-rules`
inside the api container, pipe it to `--rules` on the host.

It exists at all because a rule this repo generated was *syntactically* wrong and
nothing in the codebase could tell. On 2026-09-12 the first custom domain
produced `Host(`a`, `b`)` — the Traefik **v2** spelling. v3 takes one parameter
per matcher and rejected it, so Traefik dropped that router and the app answered
nobody for 110 seconds while every other router kept serving normally. Nothing
failed on the way there: the labels were written, the deploy succeeded, the
database was right, the unit tests passed. The only place the truth existed was
Traefik's own log, after the fact.

So the rules need an oracle that is Traefik itself, and it has to check SEMANTICS
as well as syntax. The dangerous version of this mistake is the one that parses:
drop the parentheses from the host group and
`Host(a) || Host(b) && Header(m)` is valid, and means
`Host(a) || (Host(b) && Header(m))` — the generated hostname would answer direct
traffic as well as the static host's forwards, silently un-scoping the backend.
A syntax check alone passes that, which is why every probe below asserts WHICH
router answered rather than that something did.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TRAEFIK = "traefik:v3.6"          # keep in step with coolify-proxy's image
WHOAMI = "traefik/whoami:latest"
PORT = 18097
APP, UUID = "shop", "abc123"
MARKER_HEADER, MARKER_TOKEN, PRIORITY = "X-Pironman-Backend", "1", "100"

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def run(*args):
    return subprocess.run(args, capture_output=True, text=True)


def emit_rules() -> dict:
    """The rules exactly as the platform writes them — read out of routing rather
    than retyped, or this would be checking a copy of the code under test."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    for k in ("COOLIFY_TOKEN", "COOLIFY_PROJECT", "COOLIFY_SERVER",
              "COOLIFY_DESTINATION", "PAAS_DB_PASSWORD"):
        os.environ.setdefault(k, "x")          # read at import; nothing connects
    os.environ.setdefault("PAAS_DB_HOST", "localhost")
    from app import routing
    from app.config import app_hosts

    hosts = app_hosts(APP, ["shop.example", "www.shop.example"])
    fe = routing.build_labels(
        {"traefik.http.services.web.loadbalancer.server.port": "80"},
        {APP: hosts})[f"traefik.http.routers.fe-{APP}.rule"]
    be = routing.scoped(
        {f"traefik.http.routers.http-0-{UUID}.rule":
             f"Host(`{hosts[0]}`) && PathPrefix(`/`)"},
        APP, hosts)[f"traefik.http.routers.http-0-{UUID}.rule"]
    return {"hosts": hosts, "fe": fe, "be": be,
            "marker": [routing.BACKEND_HEADER, routing.BACKEND_TOKEN],
            "priority": routing.BACKEND_PRIORITY}


def verify(spec: dict) -> int:
    hosts, fe, be = spec["hosts"], spec["fe"], spec["be"]
    header, token = spec.get("marker", [MARKER_HEADER, MARKER_TOKEN])
    print(f"\n  fe rule: {fe}\n  be rule: {be}\n")

    tmp = Path(tempfile.mkdtemp())
    (tmp / "dyn").mkdir()
    # Two DIFFERENT backends, so the response says which router matched. With one
    # service both routers answer 200 and the precedence bug is invisible.
    (tmp / "dyn" / "r.yml").write_text(json.dumps({"http": {
        "services": {
            "static-host": {"loadBalancer": {"servers": [{"url": "http://fe-echo:80"}]}},
            "backend": {"loadBalancer": {"servers": [{"url": "http://be-echo:80"}]}},
        },
        "routers": {
            "fe": {"rule": fe, "service": "static-host",
                   "entryPoints": ["http"], "priority": 10},
            "be": {"rule": be, "service": "backend", "entryPoints": ["http"],
                   "priority": int(spec.get("priority", PRIORITY))},
        }}}))

    net = f"tfx{os.getpid()}"
    tf, fe_c, be_c = f"tfx-{os.getpid()}", f"tfxfe-{os.getpid()}", f"tfxbe-{os.getpid()}"
    run("docker", "network", "create", net)
    try:
        run("docker", "run", "-d", "--rm", "--name", fe_c, "--network", net,
            "--network-alias", "fe-echo", "--hostname", "STATIC-HOST", WHOAMI)
        run("docker", "run", "-d", "--rm", "--name", be_c, "--network", net,
            "--network-alias", "be-echo", "--hostname", "BACKEND", WHOAMI)
        run("docker", "run", "-d", "--rm", "--name", tf, "--network", net,
            "-p", f"{PORT}:8099", "-v", f"{tmp / 'dyn'}:/dyn:ro", TRAEFIK,
            "--entryPoints.http.address=:8099", "--providers.file.directory=/dyn",
            "--providers.file.watch=false", "--log.level=INFO")
        time.sleep(6)

        got = run("docker", "logs", tf)
        bad = [l for l in (got.stdout + got.stderr).splitlines() if "parsing rule" in l]
        check("both rules parse", not bad, bad[0][:170] if bad else "")

        def served_by(host, marker=False):
            """Which of the two backends answered — the only thing that tells the
            `fe` router from the `be` one, since both would return 200."""
            cmd = ["curl", "-sS", "-m", "10", "-H", f"Host: {host}"]
            if marker:
                cmd += ["-H", f"{header}: {token}"]
            out = run(*cmd, f"http://127.0.0.1:{PORT}/").stdout
            for line in out.splitlines():
                if line.startswith("Hostname:"):
                    return line.split(":", 1)[1].strip()
            code = run("curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "-m",
                       "10", "-H", f"Host: {host}",
                       f"http://127.0.0.1:{PORT}/").stdout.strip()
            return f"(no route, http {code})"

        for i, h in enumerate(hosts):
            what = "generated host" if i == 0 else f"custom domain {h}"
            check(f"{what}: a client reaches the STATIC HOST",
                  served_by(h) == "STATIC-HOST", served_by(h))
            check(f"{what}: a marked forward reaches the BACKEND",
                  served_by(h, True) == "BACKEND", served_by(h, True))
        check("an unrelated host matches neither router",
              served_by("nope.example").startswith("(no route, http 404"),
              served_by("nope.example"))
    finally:
        run("docker", "kill", tf)
        run("docker", "kill", fe_c)
        run("docker", "kill", be_c)
        run("docker", "network", "rm", net)

    print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--emit-rules" in sys.argv:
        print(json.dumps(emit_rules()))
        sys.exit(0)
    if "--rules" in sys.argv:
        arg = sys.argv[sys.argv.index("--rules") + 1]
        sys.exit(verify(json.loads(sys.stdin.read() if arg == "-" else arg)))
    sys.exit(verify(emit_rules()))
