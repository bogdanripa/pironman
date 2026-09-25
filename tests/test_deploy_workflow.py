"""The generated deploy workflow, rendered and checked — including the paired
main/dev shape.

Run it directly: `python tests/test_deploy_workflow.py` (needs pyyaml and the
app's dependencies; nothing here touches the network, Docker or the database).

Two classes of bug live in a generated file like this and neither shows up until
a real push:

**It has to be valid YAML.** The template is one `dedent`ed f-string, so any
block substituted into it must be indented to the template's own level. Get that
wrong and `dedent` recomputes the common prefix from the new shortest line, which
silently re-indents the WHOLE file. Writing the dev picker did exactly that here:
every line shifted two spaces left and the step landed at column 0. The file
still looked plausible and would have failed on GitHub, not locally.

**The tags have to differ per branch.** Both apps auto-update from a moving tag,
so if the dev branch pushed `:latest` the production app's hourly sweep would
deploy a dev build — with nothing to notice, because a deploy that succeeds looks
the same whichever commit it carries. That is the whole safety property of the
paired mode and it is one string away from being lost.
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


def main():
    single = scaffold._workflow(APP, REPO, ["main"], "/api/health")
    paired = scaffold._workflow(APP, REPO, ["main"], "/api/health", dev_app=DEV)

    print("\n[both modes are valid YAML — the indentation trap]")
    for name, wf in (("single-app", single), ("paired", paired)):
        try:
            doc = yaml.safe_load(wf)
            ok = isinstance(doc, dict) and "jobs" in doc
        except Exception as e:
            doc, ok = None, False
            print("        ", str(e).splitlines()[0])
        check(f"{name} parses, with a jobs: block", ok)
        if ok:
            # `on:` is YAML 1.1 truthy, so it loads as the boolean True.
            check(f"{name} declares a push trigger", "push" in doc[True])

    print("\n[single-app mode: one app, one key, one moving tag]")
    check("targets the app directly", f"/apps/{APP}/refresh" in single)
    check("uses the unqualified PAAS_KEY", "secrets.PAAS_KEY }}" in single
          and "PAAS_KEY_DEV" not in single)
    check("pushes :latest", f"{REPO}:latest" in single)
    check("has no branch picker", "steps.target" not in single)

    print("\n[paired mode: the tags MUST differ, or dev deploys to production]")
    doc = yaml.safe_load(paired)
    check("triggers on main and dev",
          sorted(doc[True]["push"]["branches"]) == ["dev", "main"],
          str(doc[True]["push"]["branches"]))
    check("the tags block is resolved at run time, never baked",
          "${{ steps.target.outputs.moving }}" in paired
          and f"ghcr.io/bogdanripa/{REPO}:latest" not in paired)
    check("main -> the app, moving tag :latest",
          f"app={APP}; moving=latest;" in paired)
    check("dev  -> the sister app, moving tag :dev",
          f"app={DEV}; moving=dev;" in paired)
    check("each branch also gets a per-commit tag, so either can roll back",
          "commit=sha-" in paired and "commit=dev-" in paired)

    print("\n[paired mode: the deploy key]")
    check("the dev branch selects PAAS_KEY_DEV", "PAAS_KEY_DEV" in paired)
    # A secret routed through a step output would be printed into the workflow's
    # own outputs; the app id is safe there, the key is not.
    check("the key is never written to a step output",
          "key=" not in paired and "outputs.key" not in paired)

    print("\n[paired mode: every call follows the resolved app]")
    for what, frag in (("refresh", "/apps/${{ steps.target.outputs.app }}/refresh"),
                       ("health", "https://${{ steps.target.outputs.app }}")):
        check(f"the {what} call targets the resolved app", frag in paired)
    check("concurrency is per-branch, so main and dev never serialise on each "
          "other", "deploy-${{ github.ref }}" in paired)

    # app/moving/commit are shell variables of the picker step alone. A later
    # step that says "$app" gets the empty string — no error, no output, just a
    # message with a hole in it, which is the failure this repo keeps hitting.
    print("\n[the picker's shell variables do not leak into later steps]")
    picker = paired.split("- name: Pick the target app", 1)[1]
    picker, rest = picker.split("- name:", 1)
    for var in ("app", "moving", "commit"):
        check(f"no bare ${var} outside the picker step",
              re.search(r"\$" + var + r"\b", rest) is None,
              next((l.strip() for l in rest.splitlines()
                    if re.search(r"\$" + var + r"\b", l)), ""))

    print("\n[the frontend job follows the same branch as the backend]")
    fe_single = scaffold._frontend_job(*scaffold._fe_refs(APP, None))
    fe_paired = scaffold._frontend_job(*scaffold._fe_refs(APP, DEV))
    check("single-app uploads to the app itself", f"/apps/{APP}/frontend" in fe_single)
    # Resolved INLINE from github.ref_name, not from the deploy job's step
    # output: step outputs do not cross job boundaries, and the frontend job is
    # a separate job precisely so it does not wait on the image build.
    check("paired uploads to the app resolved inline",
          f"github.ref_name == 'dev' && '{DEV}' || '{APP}' }}}}/frontend" in fe_paired,
          next((l.strip()[:90] for l in fe_paired.splitlines() if "/frontend" in l), ""))
    check("and never via a cross-job step output",
          "steps.target.outputs.app }}/frontend" not in fe_paired)
    check("paired picks the matching key", "PAAS_KEY_DEV" in fe_paired)

    print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
    return 1 if fails else 0


sys.exit(main())
