"""A static site must not be handed a Docker build workflow.

Run it directly: `python tests/test_workflow_kinds.py`.

On 2026-09-24 an agent called apps_deploy_workflow for a frontend-only
repository and got the backend workflow: checkout, buildx, `docker build`,
push to ghcr, call /refresh. There is no Dockerfile in a static-site repo, so
the run died at `docker build` — minutes in, with an error naming a missing
file rather than a wrong workflow. Nothing in that error tells a reader the
workflow itself was the wrong shape, which is why it needed a human.

The shapes really are different, and the difference is not cosmetic. A static
site has no image, so it has no moving tag for the box's hourly sweep to watch,
no digest to compare, no healthcheck to wait on and no rollback to catch. The
upload IS the deploy. A frontend workflow that carried a verification step would
be verifying nothing; its absence here is the design, not an omission, and these
cases pin it so a later "consistency" tidy-up cannot quietly add one.
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for k in ("COOLIFY_TOKEN", "COOLIFY_PROJECT", "COOLIFY_SERVER",
          "COOLIFY_DESTINATION", "PAAS_DB_PASSWORD"):
    os.environ.setdefault(k, "x")
os.environ.setdefault("PAAS_DB_HOST", "localhost")

import yaml  # noqa: E402

from app.routers import scaffold  # noqa: E402

APP, DEV, REPO = "notes", "notes-dev", "notes"
fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def build(kind, build_step=False, dev=None):
    branches = ["main", "dev"] if dev else ["main"]
    return scaffold._assembled_workflow(kind, build_step, APP, REPO, branches,
                                        "/api/health", dev)


def jobs_of(text):
    return sorted((yaml.safe_load(text).get("jobs") or {}).keys())


def main():
    print("[frontend-only: no Docker anything]")
    for dev in (None, DEV):
        label = "paired" if dev else "single"
        w = build("frontend", False, dev)
        doc = yaml.safe_load(w)
        check(f"{label}: valid YAML", isinstance(doc, dict))
        check(f"{label}: the only job is the frontend upload",
              jobs_of(w) == ["frontend"], str(jobs_of(w)))
        for forbidden in ("docker build", "buildx", "ghcr.io", "/refresh",
                          "docker/build-push-action"):
            check(f"{label}: no '{forbidden}'", forbidden not in w)
        # "Dockerfile" may appear in a comment — the file says what to do if the
        # repo later grows one — but must never appear in a step that RUNS.
        live = [l for l in w.splitlines()
                if l.strip() and not l.lstrip().startswith("#")]
        check(f"{label}: nothing executable mentions a Dockerfile",
              not [l for l in live if "Dockerfile" in l],
              str([l.strip() for l in live if "Dockerfile" in l])[:120])
        check(f"{label}: it uploads a bundle", "/frontend" in w and "site.zip" in w)
        check(f"{label}: no deploy-verification step",
              "verified" not in w and "deploy=" not in w)

    print("\n[frontend-only: the build variant is chosen by the caller]")
    nobuild = build("frontend", False)
    withbuild = build("frontend", True)
    check("no-build does not run npm", "npm run build" not in nobuild)
    check("build does run npm", "npm run build" in withbuild)
    check("both are valid YAML",
          isinstance(yaml.safe_load(nobuild), dict)
          and isinstance(yaml.safe_load(withbuild), dict))

    print("\n[frontend-only, paired: the upload follows the branch]")
    w = build("frontend", False, DEV)
    check("the upload targets the app resolved inline",
          f"github.ref_name == 'dev' && '{DEV}' || '{APP}' }}}}/frontend" in w,
          next((l.strip()[:90] for l in w.splitlines() if "/frontend" in l), ""))
    check("the dev key is selected for dev", "PAAS_KEY_DEV" in w)
    check("triggers on both branches",
          sorted(yaml.safe_load(w)[True]["push"]["branches"]) == ["dev", "main"])
    check("no image tags are resolved — there is no image",
          "moving=" not in w and ":latest" not in w)
    # There is no picker step here at all any more, and that is the fix rather
    # than an omission: one inline conditional works in any job, whereas a step
    # output only works in the job that wrote it.
    check("no picker step is needed", "Pick the target app" not in w)
    check("and no step output is read", "steps.target" not in w)

    print("\n[backend: unchanged]")
    w = build("backend", False)
    check("valid YAML", isinstance(yaml.safe_load(w), dict))
    check("the only job is deploy", jobs_of(w) == ["deploy"], str(jobs_of(w)))
    check("it builds an image", "docker/build-push-action" in w)
    check("and verifies the deploy", "/refresh?deploy=" in w)
    check("byte-identical to what a plain call produces",
          w == scaffold._workflow(APP, REPO, ["main"], "/api/health", None))

    print("\n[both: one file carrying both jobs, assembled here not by hand]")
    for dev in (None, DEV):
        label = "paired" if dev else "single"
        w = build("both", True, dev)
        doc = yaml.safe_load(w)
        check(f"{label}: valid YAML — the splice did not break indentation",
              isinstance(doc, dict))
        check(f"{label}: both jobs present",
              jobs_of(w) == ["deploy", "frontend"], str(jobs_of(w)))
        check(f"{label}: the backend half still verifies", "/refresh?deploy=" in w)
        check(f"{label}: the frontend half still uploads", "site.zip" in w)

    print("\n[both, paired: every call follows the same branch]")
    w = build("both", True, DEV)
    doc = yaml.safe_load(w)
    check("backend targets the resolved app",
          "/apps/${{ steps.target.outputs.app }}/refresh" in w)
    check("frontend targets the app resolved INLINE, not via a step output",
          f"github.ref_name == 'dev' && '{DEV}' || '{APP}' }}}}/frontend" in w,
          next((l.strip()[:90] for l in w.splitlines() if "/frontend" in l), ""))

    print("\n[no job may read a step output belonging to another job]")
    # GitHub scopes step outputs to their own job. A cross-job reference is not
    # an error — it silently resolves to the empty string, so the upload PUTs to
    # /apps//frontend and only that half of the run fails. This is the check
    # that was missing: the old suite asserted the picker appeared exactly ONCE
    # and called that a feature, which is precisely the bug, and the workflow it
    # blessed failed on bogdanripa/ping-pong's dev branch on 2026-09-25.
    for kind in ("backend", "frontend", "both"):
        for dev in (None, DEV):
            text = build(kind, True, dev)
            doc = yaml.safe_load(text)
            for job, spec in (doc.get("jobs") or {}).items():
                blob = yaml.dump(spec)
                defines = {m for m in re.findall(r"id:\s*(\S+)", blob)}
                uses = set(re.findall(r"steps\.([A-Za-z0-9_-]+)\.outputs", blob))
                orphan = uses - defines
                check(f"{kind}/{'paired' if dev else 'single'}: job '{job}' "
                      f"defines every step output it reads",
                      not orphan,
                      f"reads {sorted(uses)}, defines {sorted(defines)}"
                      + (f", ORPHANED {sorted(orphan)}" if orphan else ""))

    print("\n[every generated workflow reports the commit it built]")
    # deploys_status is only better than comparing timestamps if the pipeline
    # says which commit it shipped. A workflow that does not send it leaves the
    # caller back on the guess this was meant to replace.
    for kind, needle in (("backend", '"commit": "${{ github.sha }}"'),
                         ("frontend", "?commit=${{ github.sha }}"),
                         ("both", '"commit": "${{ github.sha }}"')):
        w = build(kind, True)
        check(f"{kind} sends the commit", needle in w)
    w = build("both", True)
    check("both sends it on BOTH halves",
          '"commit": "${{ github.sha }}"' in w
          and "?commit=${{ github.sha }}" in w)

    print("\n[the shapes are genuinely different files]")
    check("frontend is much shorter than backend",
          len(build("frontend", False).splitlines())
          < len(build("backend", False).splitlines()) / 2,
          f"{len(build('frontend', False).splitlines())} vs "
          f"{len(build('backend', False).splitlines())} lines")

    print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
    return 1 if fails else 0


sys.exit(main())
