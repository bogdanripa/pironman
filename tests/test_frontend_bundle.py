"""The bundle is public, so what goes into it is a security decision.

Run it directly: `python tests/test_frontend_bundle.py`. It shells out to `zip`
when one is available, and says so when there is not.

The no-build frontend job used to zip the whole checkout minus `.git`,
`.github`, `.gitignore` and `README.md`. That is right for a repository that is
only a site and wrong for every `kind='both'` repository, where the site shares
a root with the service that serves it.

On 2026-09-25 `ping-pong` published, on its public URL, all of:

    /server.js   /package.json   /Dockerfile   /.dockerignore
    /migrations/001_leaderboard.sql            /specs/PIN-13.md

all `200 OK`, from a workflow this module generated. Nothing failed. The site
worked perfectly, which is the whole difficulty: a published source file has no
symptom. It was found only by listing the bundle while looking at something
else.

So the filter is an ALLOWLIST of web-asset extensions, not a denylist of
plumbing. The two are not equally wrong when incomplete: a gap in an allowlist
makes one asset 404, which whoever caused it sees immediately; a gap in a
denylist publishes source and says nothing at all. Fail-closed is the only
defensible direction when the cost of a miss is disclosure.

Two mechanical properties of the generated shell are pinned here as hard as the
file list, because both were wrong in the first draft and neither is visible by
reading the YAML:

  * every pattern is single-quoted — an unquoted `*.css` is expanded by the
    SHELL before zip sees it, and then matches only root-level files, silently
    dropping every asset in a subdirectory. Measured: with a `style.css` in the
    root, the unquoted form dropped `assets/app.css` and `assets/img/paddle.png`
    while the quoted form kept them;
  * every wrapped line carries its continuation — without it the exclude list
    is a separate command, which the shell tries to execute.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for k in ("COOLIFY_TOKEN", "COOLIFY_PROJECT", "COOLIFY_SERVER",
          "COOLIFY_DESTINATION", "PAAS_DB_PASSWORD"):
    os.environ.setdefault(k, "x")
os.environ.setdefault("PAAS_DB_HOST", "localhost")

import yaml  # noqa: E402

from app.routers import scaffold  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def packaging_step(publish_dir=".", extra=(), kind="both"):
    w = scaffold._assembled_workflow(kind, False, "ping-pong", "ping-pong",
                                     ["main"], "/api/health", None,
                                     publish_dir, extra)
    doc = yaml.safe_load(w)
    step = next(s for s in doc["jobs"]["frontend"]["steps"]
                if s.get("name") == "Package the bundle")
    return w, step["run"]


# ping-pong's actual repository, as the box shows its published bundle, plus the
# plumbing the upload happened to drop and two assets in a SUBDIRECTORY — the
# case the quoting bug destroys and a root-only test would never notice.
REPO = {
    ".git/config": "x", ".github/workflows/deploy.yml": "x", ".gitignore": "x",
    ".dockerignore": "x", "Dockerfile": "x", "README.md": "x",
    "package.json": "x", "package-lock.json": "x",
    "server.js": "x", "game-controls.js": "x", "favicon.svg": "x",
    "style.css": "x", "logo.png": "x",
    "migrations/001_leaderboard.sql": "x",
    "migrations/tests/leaderboard_schema.sql": "x",
    "specs/PIN-13.md": "x",
    "test/backend.test.js": "x", "test/frontend-controls.test.js": "x",
    "assets/app.css": "x", "assets/img/paddle.png": "x",
    "index.html": '<!doctype html><link rel="icon" href="/favicon.svg">'
                  '<link rel="stylesheet" href="assets/app.css">'
                  '<script src="/game-controls.js"></script>',
}

MUST_NOT_PUBLISH = ["server.js", "package.json", "package-lock.json",
                    "Dockerfile", ".dockerignore", "README.md", ".gitignore",
                    "migrations/001_leaderboard.sql",
                    "migrations/tests/leaderboard_schema.sql",
                    "specs/PIN-13.md", "test/backend.test.js",
                    "test/frontend-controls.test.js",
                    ".git/config", ".github/workflows/deploy.yml"]
MUST_PUBLISH = ["index.html", "favicon.svg", "game-controls.js", "style.css",
                "logo.png", "assets/app.css", "assets/img/paddle.png"]


def run_packaging(run_block, repo=REPO):
    """Build the repo in a temp dir and execute the generated step verbatim."""
    root = Path(tempfile.mkdtemp())
    site, work = root / "repo", root / "work"
    work.mkdir()
    for path, body in repo.items():
        f = site / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    subprocess.run(["sh", "-c", run_block], cwd=site, check=True,
                   env={**os.environ, "GITHUB_WORKSPACE": str(work)},
                   capture_output=True)
    out = subprocess.run(["unzip", "-Z1", str(work / "site.zip")],
                         capture_output=True, text=True, check=True)
    return sorted(x for x in out.stdout.split() if not x.endswith("/"))


def main():
    print("[the generated shell is shell-correct]")
    _, run = packaging_step(extra=("server.js",))
    zip_lines = [l for l in run.splitlines()
                 if l.strip().startswith(("zip ", "-i ", "-x ", "'"))
                 or (l.strip().startswith("'") and "'" in l)]
    # Every wrapped line of the zip command must continue, or the rest is run
    # as a command of its own.
    body = run.split("zip -qr", 1)[1].split("\n#", 1)[0].rstrip()
    cont = [l for l in body.splitlines()[:-1]]
    check("every wrapped line of the zip command continues",
          all(l.rstrip().endswith("\\") for l in cont),
          str([l.strip()[:40] for l in cont if not l.rstrip().endswith("\\")]))
    # Every pattern quoted. An unquoted one is expanded by the shell first.
    pats = re.findall(r"(?<![\w'])(\*\.[A-Za-z0-9]+)(?![\w'])", body)
    check("no pattern is left unquoted for the shell to expand", not pats,
          f"unquoted: {sorted(set(pats))}")
    check("it lists the bundle on every run", "unzip -l" in run)
    # _root_backend_files rediscovers files the static list already names —
    # package.json is both repo plumbing and a root .json no page references —
    # and a pattern printed twice reads like a bug to whoever is looking for one.
    _, dup = packaging_step(extra=("package.json", "server.js"))
    quoted = re.findall(r"'([^']+)'", dup.split("-x", 1)[1])
    check("no exclude pattern is repeated",
          len(quoted) == len(set(quoted)),
          str([p for p in set(quoted) if quoted.count(p) > 1]))

    if not (shutil.which("zip") and shutil.which("unzip")):
        print("\n  -- zip/unzip not available; skipping the packaging runs --")
    else:
        print("\n[running it: the ping-pong repo, packaged from the root]")
        names = run_packaging(run)
        for bad in MUST_NOT_PUBLISH:
            check(f"{bad} is NOT published", bad not in names)
        for good in MUST_PUBLISH:
            check(f"{good} is published", good in names,
                  f"bundle: {names}")

        print("\n[a publish_dir sidesteps the whole question]")
        repo = {**REPO, "public/index.html": "<!doctype html>",
                "public/app.js": "x"}
        _, run_dir = packaging_step(publish_dir="public")
        names = run_packaging(run_dir, repo)
        check("only the directory's contents are published",
              names == ["app.js", "index.html"], str(names))
        check("index.html is at the zip root, not under public/",
              "index.html" in names and "public/index.html" not in names)

        print("\n[an unreferenced root script is not published; a used one is]")
        # The distinction the whole exclusion rests on, end to end.
        check("server.js out, game-controls.js in",
              "server.js" not in run_packaging(run)
              and "game-controls.js" in run_packaging(run))

    print("\n[what counts as referenced]")
    refs = scaffold._referenced_names(REPO["index.html"])
    check("a src= is a reference", "game-controls.js" in refs, str(refs))
    check("an href= is a reference", "favicon.svg" in refs, str(refs))
    check("a path is reduced to its basename", "app.css" in refs, str(refs))
    mod = scaffold._referenced_names(
        "import x from './helper.js';\nconst y = await import('./lazy.js');")
    check("a module import is a reference", "helper.js" in mod, str(mod))
    check("a dynamic import too", "lazy.js" in mod, str(mod))
    q = scaffold._referenced_names('<script src="/a.js?v=2#x"></script>')
    check("query and fragment are stripped", q == {"a.js"}, str(q))

    print("\n[which root scripts belong to the backend, asked of the repo]")
    # Name-matching is what this deliberately does NOT do: `index.js` and
    # `main.js` are as often a site's entry point as a server's, so a list of
    # likely server filenames would break real sites to protect differently
    # spelled ones. The test is reachability from an HTML page instead.
    import asyncio

    def repo(files, pages):
        class FakeGH:
            GitHubError = scaffold.github_api.GitHubError

            @staticmethod
            async def list_dir(owner, name, path=""):
                return [{"name": f, "type": "file"} for f in files]

            @staticmethod
            async def read_file(owner, name, path):
                return pages.get(path)
        scaffold.github_api = FakeGH
        return asyncio.run(scaffold._root_backend_files("o/r"))

    real = scaffold.github_api
    try:
        out = repo(["index.html", "server.js", "game-controls.js",
                    "package.json"],
                   {"index.html": '<script src="/game-controls.js"></script>'})
        check("the unreferenced server script is named",
              "server.js" in out, str(out))
        check("package.json too", "package.json" in out, str(out))
        check("but never the script the page loads",
              "game-controls.js" not in out, str(out))

        out = repo(["index.html", "main.js", "boot.js"],
                   {"index.html": '<script type="module" src="main.js">'
                                  '</script>',
                    "main.js": "import './boot.js';"})
        check("a module's own import survives one hop", out == (), str(out))

        out = repo(["index.html", "index.js"],
                   {"index.html": '<script src="index.js"></script>'})
        check("index.js is kept when the page uses it", out == (), str(out))
        out = repo(["index.html", "index.js"], {"index.html": "<p>hi</p>"})
        check("and dropped when it does not", out == ("index.js",), str(out))

        out = repo(["index.html", "favicon.svg", "style.css"],
                   {"index.html": "<p>hi</p>"})
        check("nothing is named when there are no scripts at all",
              out == (), str(out))

        class Blind:
            GitHubError = scaffold.github_api.GitHubError

            @staticmethod
            async def list_dir(owner, name, path=""):
                raise scaffold.github_api.GitHubError("403")
        scaffold.github_api = Blind
        check("an unreadable repo excludes nothing rather than guessing",
              asyncio.run(scaffold._root_backend_files("o/r")) == ())
    finally:
        scaffold.github_api = real

    print("\n[the build variant is untouched — it already ships only its output]")
    w = scaffold._assembled_workflow("both", True, "notes", "notes", ["main"],
                                     "/api/health", None)
    doc = yaml.safe_load(w)
    step = next(s for s in doc["jobs"]["frontend"]["steps"]
                if s.get("name") == "Package the bundle")
    check("it still zips the build output", "cd dist" in step["run"],
          step["run"][:60])
    check("and carries no allowlist, having nothing to filter",
          "-i " not in step["run"])

    print("\n[every shape still produces valid YAML and one frontend job]")
    for kind in ("frontend", "both"):
        for pd, extra in ((".", ("server.js",)), ("public", ())):
            w = scaffold._assembled_workflow(kind, False, "notes", "notes",
                                             ["main", "dev"], "/api/health",
                                             "notes-dev", pd, extra)
            doc = yaml.safe_load(w)
            check(f"{kind}/{pd}: parses and has a frontend job",
                  isinstance(doc, dict) and "frontend" in doc["jobs"])
            # The invariant from test_workflow_kinds must survive this change.
            for job, spec in doc["jobs"].items():
                blob = yaml.dump(spec)
                defines = set(re.findall(r"id:\s*(\S+)", blob))
                uses = set(re.findall(r"steps\.([A-Za-z0-9_-]+)\.outputs", blob))
                check(f"{kind}/{pd}: job '{job}' defines what it reads",
                      not (uses - defines), f"orphaned {sorted(uses - defines)}")

    print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
    return 1 if fails else 0


sys.exit(main())
