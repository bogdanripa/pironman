from fastapi import APIRouter, Depends, HTTPException
import json
from typing import Literal
from textwrap import dedent, indent

from ..auth import require_key
from ..db import pool
from ..config import app_url, DOMAIN_SUFFIX, GHCR_OWNER
from .. import github_api

router = APIRouter(prefix="/apps", tags=["apps"],
                   dependencies=[Depends(require_key)])


def _upload_step(app_ref: str, key_ref: str) -> str:
    """The last step of either frontend job. Authenticated with PAAS_KEY, which
    this platform installs itself — unlike the backend's /refresh hook this
    uploads real content, so it cannot be an unauthenticated call."""
    return dedent(f"""\
        # PAAS_KEY is this app's scoped deploy key. It is already in the repo:
        # apps_create returns it and github_secret_set installs it. Nothing for
        # a human to paste.
        #
        # ?commit= is what lets deploys_status say WHICH build is live. Without
        # it the only way to answer that is to compare an upload time against a
        # commit time, which is a guess — and a wrong one whenever a deploy was
        # rolled back, because then the newer timestamp belongs to the older
        # build.
        - name: Upload the frontend
          run: |
            curl -fsS -X PUT \\
              -H "Authorization: Bearer {key_ref}" \\
              --data-binary @site.zip \\
              "https://api-coolify.bogdanripa.com/apps/{app_ref}/frontend?commit=${{{{ github.sha }}}}"
        """)


def _frontend_job(app_ref: str, key_ref: str) -> str:
    """The optional frontend half of the workflow, for a site with a build step:
    build static assets, zip them, upload. Separate job so an app can have a
    frontend, a backend, or both — and so a frontend deploy doesn't wait on an
    image build. Indented two spaces so it drops straight in under the workflow's
    `jobs:` key."""
    # Concatenated rather than interpolated: dedent() runs after an f-string is
    # built, so a multi-line value spliced in mid-template changes the common
    # indent it strips and silently mangles the YAML.
    return indent(dedent("""\
        frontend:
          runs-on: ubuntu-latest
          steps:
            - uses: actions/checkout@v4
            - uses: actions/setup-node@v4
              with:
                node-version: '22'

            # Adjust to your project (working-directory, build command, and the
            # output dir — 'dist' for Vite, 'build' for CRA/Next export).
            - run: npm ci
            - run: npm run build

            # Zip the CONTENTS of the build output: index.html must be at the
            # zip's root.
            - name: Package the bundle
              run: cd dist && zip -qr "$GITHUB_WORKSPACE/site.zip" .

        """) + indent(_upload_step(app_ref, key_ref), "    "), "  ")


def _frontend_job_no_build(app_ref: str, key_ref: str) -> str:
    """The same job for a site that has no build step — the files in the repo are
    the site. A game, a landing page, a status page: plain HTML/CSS/JS, which is
    also what apps_frontend_write publishes. Running `npm ci && npm run build`
    against one of those fails on a missing package.json or a missing script."""
    return indent(dedent("""\
        frontend:
          runs-on: ubuntu-latest
          steps:
            - uses: actions/checkout@v4

            # No build step: the checked-out files ARE the site. Point `cd` at the
            # directory holding index.html — '.' when that is the repo root, in
            # which case the -x list keeps repo plumbing out of the bundle.
            # index.html must end up at the zip's root.
            - name: Package the bundle
              run: |
                cd .
                zip -qr "$GITHUB_WORKSPACE/site.zip" . \\
                  -x '.git/*' '.github/*' '.gitignore' 'README.md'

        """) + indent(_upload_step(app_ref, key_ref), "    "), "  ")


def _frontend_workflow(app_id: str, branches: list[str], build: bool,
                       dev_app: str | None = None) -> str:
    """A COMPLETE workflow for a repository that ships only a static site.

    Not the backend workflow with the frontend job bolted on: a frontend-only
    repo has no Dockerfile, so every step of the backend job fails — and it
    fails at `docker build`, minutes in, with an error about a missing file
    rather than about the workflow being the wrong shape. An agent handed that
    workflow for a static site has no way to tell it was given the wrong one.

    There is no image, so there is no moving tag, nothing for the box's hourly
    sweep to watch, and no deploy to verify: the upload IS the deploy, and the
    static host serves the new bundle on the next request. That is why this file
    is so much shorter than the backend one, and the shortness is the point
    rather than an omission.
    """
    job = (_frontend_job(*_fe_refs(app_id, dev_app)) if build
           else _frontend_job_no_build(*_fe_refs(app_id, dev_app)))
    head = dedent(f"""\
        name: deploy

        on:
          push:
            branches: [{", ".join(branches)}]

        # One upload at a time per branch: two overlapping runs would both PUT a
        # bundle and the one that finishes last wins, regardless of which commit
        # is newer.
        concurrency:
          group: deploy-${{{{ github.ref }}}}
          cancel-in-progress: true

        # A static site has no container and no image. The upload is the whole
        # deploy — there is no digest to watch, no healthcheck to wait on and no
        # rollback to catch, which is why there is no verification step here and
        # why its absence is not an oversight. If this repo later grows a
        # Dockerfile, call apps_deploy_workflow again with kind='both' rather
        # than hand-editing this file.

        jobs:
        """)
    # No picker step: _fe_refs resolves the app inline from github.ref_name, so
    # the job needs nothing injected into it and the same job body works whether
    # this file is the whole workflow or one job beside the backend's.
    return head + job


def _fe_refs(app_id: str, dev_app: str | None) -> tuple[str, str]:
    """(app_ref, key_ref) for a frontend job, both resolved INLINE from
    `github.ref_name`.

    Not from `steps.target.outputs.app`, which is what this used to do and which
    cannot work: **step outputs are scoped to the job that produced them**. The
    picker step lives in the `deploy` job, so a separate `frontend` job asking
    for its output gets the empty string — and the upload then PUTs to
    `/apps//frontend`, which is not this app's URL and fails. The backend half
    succeeded in the same run, so the workflow looked half-working rather than
    wrong. Reported from a real run on bogdanripa/ping-pong's dev branch,
    2026-09-25.

    The inline conditional needs no step, no `needs:` edge and no job output, so
    it works in any job and keeps the two jobs independent — which is the point
    of their being separate jobs at all: a frontend deploy must not wait on an
    image build. It is also the form the deploy key has always used one line
    below, so this makes the pair consistent rather than introducing a new
    mechanism.
    """
    if not dev_app:
        return app_id, "${{ secrets.PAAS_KEY }}"
    return (f"${{{{ github.ref_name == 'dev' && '{dev_app}' || '{app_id}' }}}}",
            "${{ github.ref_name == 'dev' "
            "&& secrets.PAAS_KEY_DEV || secrets.PAAS_KEY }}")


def _sha_tag_step() -> str:
    """Single-app mode: one moving tag (:latest) plus the commit sha."""
    return dedent("""\
        - id: tag
          run: echo "value=sha-${GITHUB_SHA::7}" >> $GITHUB_OUTPUT
        """)


def _target_step(app_id: str, dev_app: str) -> str:
    """Paired mode: resolve which app, and which tags, this push is for.

    Emitted as step outputs rather than baked into the file so one workflow
    serves both branches. The app id is safe to put in an output; the deploy key
    is not, which is why that is selected inline at each use instead.
    """
    return dedent(f"""\
        # main -> {app_id} (:latest), dev -> {dev_app} (:dev). The two share a
        # repository and a Dockerfile and nothing else: separate hostname,
        # database, environment, schedule and release cadence.
        #
        # The tags MUST differ. Both apps auto-update from their moving tag, so a
        # dev build pushed to :latest would be deployed to production by the
        # box's hourly sweep — with nothing to notice, because a deploy that
        # succeeds looks the same whichever commit it carries.
        - name: Pick the target app
          id: target
          run: |
            if [ "${{{{ github.ref_name }}}}" = "dev" ]; then
              app={dev_app}; moving=dev;    commit=dev-${{GITHUB_SHA::7}}
            else
              app={app_id}; moving=latest; commit=sha-${{GITHUB_SHA::7}}
            fi
            echo "app=$app"       >> $GITHUB_OUTPUT
            echo "moving=$moving" >> $GITHUB_OUTPUT
            echo "commit=$commit" >> $GITHUB_OUTPUT
            echo "branch ${{{{ github.ref_name }}}} -> app $app, tags :$moving and :$commit"
        """)


def _workflow(app_id: str, repo_name: str, branches: list[str],
              health_path: str, dev_app: str | None = None) -> str:
    """The deploy workflow. With `dev_app` it serves TWO apps from one repository
    — `main` deploys the production app, `dev` deploys its sister — and the file
    stays single, because everything that differs is resolved at run time from
    `github.ref_name`.

    The image TAGS are the load-bearing part of that, not a nicety. Both apps
    auto-update from a moving tag, so if the dev branch pushed `:latest` the
    production app's hourly sweep would pick up a dev build and deploy it —
    silently, because a deploy that succeeds looks identical whichever commit it
    carries. main therefore owns `:latest`, dev owns `:dev`, and each also gets a
    per-commit tag so either side can be rolled back.

    The deploy key cannot come from a step output the way the app id does: that
    would print a secret into the workflow's own outputs. It is selected inline
    at each use instead, which keeps it in the secrets context throughout.
    """
    if dev_app:
        branches = [*branches, "dev"] if "dev" not in branches else list(branches)
        app_ref = "${{ steps.target.outputs.app }}"
        key_ref = ("${{ github.ref_name == 'dev' "
                   "&& secrets.PAAS_KEY_DEV || secrets.PAAS_KEY }}")
        moving_ref = "${{ steps.target.outputs.moving }}"
        commit_ref = "${{ steps.target.outputs.commit }}"
    else:
        app_ref = app_id
        key_ref = "${{ secrets.PAAS_KEY }}"
        moving_ref = "latest"
        commit_ref = "${{ steps.tag.outputs.value }}"
    # Three comments and one error message are worded per mode, and they are
    # rewrapped by hand rather than generated so that a single-app file comes
    # out byte-identical to the one this generator has always produced. A
    # regenerated workflow that differs only in prose reads as a real change in
    # a diff, which is a cost paid by every repo that re-runs this tool.
    C = "\n        # "            # the top-level comment's continuation
    K = "\n              # "      # inside a step
    race_note = (
        f"Every run pushes the same moving tag for its branch — :latest on{C}"
        f"main, :dev on dev — and the box deploys whatever that points at, so{C}"
        f"two overlapping runs on ONE branch race and the one that FINISHES{C}"
        f"last wins regardless of which commit is newer: a quick follow-up{C}"
        f"commit can land first and then be undone by its predecessor. The two{C}"
        f"branches never race each other, because they move different tags.{C.rstrip(' ')}"
        if dev_app else
        f"Every run pushes the same :latest tag and the box deploys whatever that{C}"
        f"points at, so two overlapping runs race and the one that FINISHES last{C}"
        f"wins regardless of which commit is newer — a quick follow-up commit can{C}"
        "land first and then be undone by its predecessor.")
    # The secret reference is an inline conditional, so a reader meeting
    # PAAS_KEY_DEV halfway through a line needs to know it is a whole second app
    # being deployed and not a variant credential.
    key_note = (
        f"Authenticated with the{K}"
        f"TARGET app's scoped deploy key — PAAS_KEY on main, PAAS_KEY_DEV on{K}"
        f"dev — each of which can only deploy its own app. The control plane{K}"
        f"redeploys itself often and answers 404 while it restarts, so{K}"
        "transient codes are retried rather than believed."
        if dev_app else
        f"Authenticated with this{K}"
        f"app's scoped deploy key (PAAS_KEY), which can only deploy this one{K}"
        f"app. The control plane redeploys itself often and answers 404{K}"
        f"while it restarts, so transient codes are retried rather than{K}"
        "believed.")
    # A key scoped to the sibling is the likely mistake once there are two, and
    # it fails here with exactly the 401 a missing secret gives.
    key_401 = (f"the key is not {app_ref}'s - on dev that secret is "
               "PAAS_KEY_DEV, on main PAAS_KEY"
               if dev_app else "PAAS_KEY is not this app's deploy key")
    tag_step = _target_step(app_id, dev_app) if dev_app else _sha_tag_step()
    branch_list = ", ".join(branches)
    return dedent(f"""\
        name: deploy
        on:
          push:
            branches: [{branch_list}]

        # {race_note} Build times still vary
        # with image size, so don't build a polling timeout around a guess; time
        # your own first run and use that. One run at a time, newest wins.
        concurrency:
          group: deploy-${{{{ github.ref }}}}
          cancel-in-progress: true

        jobs:
          deploy:
            # NATIVE arm64, matching the Pi, rather than an x86 runner emulating
            # it. Cross-building under QEMU runs every dependency install and
            # compile step emulated, which is the difference between a couple of
            # minutes and half an hour. GitHub's arm64 runners are free for PUBLIC
            # repositories; on a private repo they need a paid plan, and the job
            # will sit waiting for a runner that never comes. If that happens, put
            # `ubuntu-latest` back and re-add `- uses: docker/setup-qemu-action@v3`
            # above setup-buildx.
            runs-on: ubuntu-24.04-arm
            permissions:
              contents: read
              packages: write
            steps:
              - uses: actions/checkout@v4

              - uses: docker/setup-buildx-action@v3

              - uses: docker/login-action@v3
                with:
                  registry: ghcr.io
                  username: ${{{{ github.actor }}}}
                  password: ${{{{ secrets.GITHUB_TOKEN }}}}

{indent(tag_step.rstrip(), "              ")}

              - uses: docker/build-push-action@v6
                with:
                  platforms: linux/arm64
                  push: true
                  # Without this every build is cold: nothing carries between
                  # runs, so a dependency install repeats in full even when its
                  # lockfile has not moved. Keyed on the layer inputs, so a real
                  # change still rebuilds.
                  cache-from: type=gha
                  cache-to: type=gha,mode=max
                  # So the image can report which commit it is running. A health
                  # endpoint that answers "unknown" cannot tell you whether a
                  # deploy actually landed, which is exactly when you want to
                  # know. Ignored harmlessly if the Dockerfile declares no such
                  # ARG.
                  build-args: |
                    GIT_SHA=${{{{ github.sha }}}}
                  tags: |
                    ghcr.io/{GHCR_OWNER}/{repo_name}:{moving_ref}
                    ghcr.io/{GHCR_OWNER}/{repo_name}:{commit_ref}

              # Tells the box what this build pushed. On the app's FIRST deploy
              # that image is what creates its container — the app was registered
              # as a bare id, because only this pipeline knows what it runs. After
              # that the box watches the tag itself and this just asks it to check
              # now rather than at the next hourly sweep. {key_note}
              #
              # 202 means QUEUED, not deployed. The hook used to hold the
              # connection open until it had verified the deploy, but Cloudflare
              # fronts that API and cuts a request at ~100s, so the deploys worth
              # hearing about were the ones this step could not hear about — two
              # measured at 125s, both answered 524, both hiding a real verdict.
              # It now returns a deploy id and the next step collects the result.
              - name: Trigger deploy on the Pi
                id: refresh
                run: |
                  for i in $(seq 1 10); do
                    code=$(curl -sS -o /tmp/out -w '%{{http_code}}' -X POST \\
                      -H "Authorization: Bearer {key_ref}" \\
                      -H "Content-Type: application/json" \\
                      -d '{{"image": "ghcr.io/{GHCR_OWNER}/{repo_name}:{moving_ref}", "commit": "${{{{ github.sha }}}}"}}' \\
                      "https://api-coolify.bogdanripa.com/apps/{app_ref}/refresh" || echo 000)
                    case "$code" in
                      2*) cat /tmp/out; echo
                          python3 -c "import json;print('id='+json.load(open('/tmp/out')).get('deploy',''))" \\
                            >> $GITHUB_OUTPUT
                          exit 0 ;;
                      404|000|503|504) echo "attempt $i: $code, retrying"; sleep 10 ;;
                      401) echo "{key_401}"; exit 1 ;;
                      *)  cat /tmp/out; echo; echo "refresh failed ($code)"; exit 1 ;;
                    esac
                  done
                  echo "gave up after 10 attempts"; exit 1

              # The verdict the 202 could not carry. This is the ONLY step that
              # can tell a rolled-back deploy from a good one: Coolify rolls a
              # failed deploy back silently and the PREVIOUS container keeps
              # serving, so the app answers its healthcheck perfectly well with
              # the old code still running. Health polling cannot see that. This
              # can, because the box compared container start times.
              #
              # 'unknown' is not failed. It means the control plane was recycled
              # mid-deploy and never wrote the outcome down — real, since the
              # control plane is itself an app on this box. Guessing 'failed'
              # there would red a good build, so this warns and lets the health
              # check below have the last word.
              - name: Wait for the deploy to be verified
                run: |
                  url="https://api-coolify.bogdanripa.com/apps/{app_ref}/refresh?deploy=${{{{ steps.refresh.outputs.id }}}}"
                  for i in $(seq 1 60); do
                    body=$(curl -sS -H "Authorization: Bearer {key_ref}" "$url" || echo '{{}}')
                    state=$(printf '%s' "$body" | python3 -c "import json,sys;print(json.load(sys.stdin).get('state',''))" 2>/dev/null || echo)
                    case "$state" in
                      succeeded) echo "deploy verified on the box"; exit 0 ;;
                      failed) printf '%s\\n' "$body"
                              echo "the new container never came up healthy, so Coolify rolled"
                              echo "the deploy back — the PREVIOUS version is still serving and"
                              echo "will answer the health check below. check: apps_logs {app_ref}"
                              exit 1 ;;
                      running|"") sleep 5 ;;
                      unknown) printf '%s\\n' "$body"
                               echo "::warning::the box lost track of this deploy; falling through"
                               echo "to the health check, which cannot detect a rollback"
                               exit 0 ;;
                    esac
                  done
                  echo "::warning::no verdict after 5 minutes; falling through to the health check"

              # A second opinion on top of the server-side verdict: this one also
              # catches an app that starts, reports healthy and still serves
              # errors on the path that matters.
              #
              # It requests the app's health_path ({health_path}), not '/', and
              # requires a 2xx. That distinction matters on an app that also has a
              # frontend: '/' is answered by the static bundle straight from the
              # CDN with no container in the path, so a '/' check goes green even
              # when the backend is dead. Give such an app a health_path its
              # BACKEND owns (apps_create health_path, or apps_update) and this
              # step tests something.
              - name: Wait for the new version to be healthy
                run: |
                  url="https://{app_ref}{DOMAIN_SUFFIX}{health_path}"
                  for i in $(seq 1 40); do
                    code=$(curl -s -o /dev/null -w '%{{http_code}}' "$url" || echo 000)
                    case "$code" in
                      2*) echo "up (HTTP $code)"; exit 0 ;;
                      # Each of these is retried rather than failed, because
                      # each is something a deploy still in progress produces.
                      # 404: mid-rollover the request can still reach the
                      # outgoing container. 500: an app with
                      # is_consistent_container_name_enabled cannot run two
                      # containers at once, so its deploy stops the old one
                      # before starting the new, and the edge answers 500 across
                      # that gap. Measured on wa-gateway, run 32463472876 -- this
                      # step sampled once, hit the gap, and failed a deploy that
                      # had actually succeeded: the container was already up,
                      # healthy, and serving the new image.
                      #
                      # Retrying 500 gives up no failure detection. The loop is
                      # bounded at 40 attempts x 6s, so an app genuinely serving
                      # 500 still fails, about four minutes later. All this stops
                      # is one transient sample being treated as terminal, which
                      # is already how 502, 503 and 504 are handled -- 500 was
                      # the inconsistent one.
                      000|404|500|502|503|504) echo "waiting ($code)"; sleep 6 ;;
                      *) echo "$url answered HTTP $code — the health path must "
                         echo "return 2xx without authentication"; exit 1 ;;
                    esac
                  done
                  echo "no 2xx from $url — either the app did not come up"
                  echo "(check: apps_logs {app_ref}) or the app does not serve"
                  echo "{health_path}; fix the route or change health_path"
                  exit 1
        """)


def _dockerfile_rules(health_path: str) -> str:
    return dedent(f"""\
    The image must satisfy these, or the container fails to start or fails its
    healthcheck and is rolled back — while the deploy call still looks like it
    succeeded, so verify with apps_logs after deploying:

    1. Built for linux/arm64 (the workflow handles this via the `platforms` key).
    2. Listen on port 80 on BOTH IP families — NOT 0.0.0.0 alone, NOT 127.0.0.1,
       and beware that ':: ' is not automatically dual-stack in every runtime.
       Two different failures come from getting this wrong, because two different
       clients connect: the healthcheck runs INSIDE the container against
       http://localhost:80{health_path} (which resolves to ::1, IPv6 first),
       while the proxy connects from OUTSIDE to the container's IPv4 address.
         - IPv4-only (0.0.0.0): the healthcheck is refused, so the deploy is
           rolled back even though the app serves fine from outside.
         - IPv6-only: the healthcheck passes and the container reports healthy,
           but the proxy is refused and every request 502s. This is the nastier
           one — the app looks perfectly healthy while serving nothing.
       Node's app.listen(80, '::') IS dual-stack. **Python is not**: asyncio sets
       IPV6_V6ONLY, so `uvicorn --host ::` listens on IPv6 only. In Python, bind
       the socket yourself and hand it to the server:
           sock = socket.create_server(("::", 80), family=socket.AF_INET6,
                                       dualstack_ipv6=True)
           uvicorn.run("app:app", fd=sock.fileno())
       If unsure, verify from outside the container, not just via localhost.
       Read the host from the environment rather than hardcoding '::' —
       app.listen(port, process.env.HOST || '::') — so the same code still runs
       on a dev box with no IPv6, where a literal '::' throws EAFNOSUPPORT. The
       container needs no HOST set; the default is the one that matters here.
    3. Run as root, or grant CAP_NET_BIND_SERVICE — binding port 80 is
       privileged. A `USER node`-style line makes the app die at startup with
       EACCES, which looks like any other "won't start". Drop the USER line.
    4. A HEALTHCHECK instruction so the platform can tell a started container
       from a ready one. The base image must contain curl or wget (Coolify tries
       curl, then wget) — the plain slim/alpine, scratch and distroless images do
       NOT ship curl, so install it.

       There is only ONE health path, and it is the app's `health_path`
       (currently **{health_path}**, set by apps_create and changeable with
       apps_update; apps_get echoes it back). The platform configures the
       container's healthcheck from it when the container is first created, and
       that configured check is what Coolify runs and what a rollback is decided
       on. The HEALTHCHECK line in your Dockerfile is the fallback that remains
       if that configuration call did not land — so point it at the same path
       rather than at '/'. Two different paths is the one combination to avoid:
       whichever check runs, it tests a route you did not mean.

       **Keep `--start-interval` on that line even though it looks redundant.**
       It is the one healthcheck setting Coolify cannot express: its API has no
       field for it, and a `--health-start-interval` passed through
       `custom_docker_run_options` is silently dropped by Coolify's
       docker-run-to-compose conversion, which allowlists 15 flags and no
       `--health-*` among them. Docker's own default is **5s**, and that is the
       delay before the FIRST probe runs — `--interval` governs only later ones.
       For an app that sleeps, that 5s is most of a cold wake: measured on this
       box, a container was answering on its own IP at 0.76s but was not marked
       healthy until 5.13s, and Sablier will not report it ready until it is.
       At `--start-interval=250ms` the same wake resolves in ~0.5s. So the line
       only bites for an app whose Coolify healthcheck is disabled — but it
       costs nothing to carry, and it is unsettable anywhere else.

    Example (note the curl install, the env-overridable :: bind, the
    HEALTHCHECK on {health_path} rather than '/', and --start-interval):

        FROM node:22-slim
        RUN apt-get update && apt-get install -y --no-install-recommends curl \\
            && rm -rf /var/lib/apt/lists/*
        WORKDIR /srv
        COPY package*.json ./
        RUN npm ci --omit=dev
        COPY . .
        ENV PORT=80
        EXPOSE 80
        # In server.js: app.listen(process.env.PORT || 80,
        #                          process.env.HOST || '::')
        # The '::' default is dual-stack in Node — an IPv4-only bind fails the
        # healthcheck — and reading HOST keeps the same file runnable on a dev
        # box without IPv6.
        HEALTHCHECK --interval=10s --timeout=3s --start-period=10s \\
          --start-interval=250ms \\
          CMD curl -fsS http://localhost:80{health_path} || exit 1
        CMD ["node", "server.js"]
    """)


def _assembled_workflow(kind: str, build: bool, app_id: str, repo: str,
                        branches: list[str], health_path: str,
                        dev_app: str | None) -> str:
    """The one file to write, complete for this repository's shape.

    'both' returns the backend workflow with the frontend job already in it,
    rather than leaving it in optional_frontend_job for the caller to splice.
    Splicing YAML by hand is where indentation goes wrong, and a workflow whose
    indentation is wrong fails on GitHub, not locally — so the assembly happens
    here, once, where a test can see it.
    """
    if kind == "frontend":
        return _frontend_workflow(app_id, branches, build, dev_app)
    backend = _workflow(app_id, repo, branches, health_path, dev_app)
    if kind != "both":
        return backend
    job = (_frontend_job(*_fe_refs(app_id, dev_app)) if build
           else _frontend_job_no_build(*_fe_refs(app_id, dev_app)))
    return backend.rstrip("\n") + "\n\n" + job


async def _detect_kind(repo: str) -> tuple[str, str]:
    """Ask the repository what shape it is. Returns (kind, evidence).

    The rule is the Dockerfile, because that is what the backend half of the
    workflow actually needs: no Dockerfile, no `docker build`, so a backend
    workflow cannot work however the repo is described. A repo that has one is
    called 'backend' rather than 'both' on purpose — a Dockerfile plus a
    package.json is the ordinary shape of a Node service that serves its own
    assets, and guessing 'both' there would add a frontend job uploading a
    bundle the app never asked to have hosted separately. Say kind='both'
    explicitly for that.

    A repository the platform cannot read is NOT reported as frontend-only.
    "No Dockerfile" and "no answer" are different facts, and collapsing them is
    how a private repo silently gets the wrong workflow.
    """
    owner, _, name = repo.partition("/") if "/" in repo else (GHCR_OWNER, "", repo)
    name = name or repo
    try:
        dockerfile = await github_api.has_file(owner, name, "Dockerfile")
    except github_api.GitHubError as e:
        raise HTTPException(
            502, f"could not read {owner}/{name} to work out whether it ships a "
                 f"container ({e}). Pass kind='frontend', 'backend' or 'both' "
                 f"explicitly and this call needs no repository access.")
    if dockerfile:
        return "backend", f"{owner}/{name} has a Dockerfile"
    return "frontend", f"{owner}/{name} has no Dockerfile"


async def _detect_build(repo: str) -> bool:
    """Whether the static site has a build step, i.e. a package.json with a
    `build` script. Wrong either way is a loud failure at the first run — `npm
    run build` against a repo with no script, or a bundle of unbuilt sources —
    so it is worth one API call rather than a coin flip. Unreadable means
    "assume no build", which is the variant that fails on a missing file rather
    than publishing sources as if they were a site."""
    owner, _, name = repo.partition("/") if "/" in repo else (GHCR_OWNER, "", repo)
    name = name or repo
    try:
        pkg = await github_api.read_file(owner, name, "package.json")
    except github_api.GitHubError:
        return False
    if not pkg:
        return False
    try:
        return "build" in (json.loads(pkg).get("scripts") or {})
    except ValueError:
        return False


@router.get("/{app_id}/deploy-workflow", operation_id="apps_deploy_workflow",
            summary="Get the GitHub Actions workflow that redeploys this app on every push")
async def deploy_workflow(app_id: str, repo_name: str | None = None,
                          dev_app: str | None = None,
                          kind: Literal["frontend", "backend", "both"] | None = None):
    """Return everything needed to wire an app up to automatic deployment from
    GitHub: the complete workflow file, where to save it, which repository
    secret to create, and the constraints its Dockerfile must satisfy.

    This is the one supported way to build and deploy an app. Never build the
    image locally and push it to the registry by hand, and never go looking for
    registry credentials to do so — that is not how apps ship here.

    Call this straight after creating an app, or whenever the user asks to "set
    up deploys", "add CI", or "make it deploy on push". Write the returned
    `workflow` content verbatim to the returned `workflow_path` in the app's own
    repository — do not compose a workflow from memory, because the image tag
    scheme, the arm64 platform flag and the redeploy call all have to match what
    this platform expects.

    **Verbatim means verbatim — so pass what you need as parameters instead of
    editing the result.** The only thing that legitimately varies per repository
    is `repo_name`, for a repository named differently from the app. Hand-editing
    the generated file is how the parts that must not change get changed by
    accident.

    **The trigger branches are not configurable, and `dev` is the only addition
    to `main`.** There used to be a `branches` parameter here, with advice to
    "pass the branch actually being worked on" so a feature-branch push wouldn't
    look like a silently-missing run. That advice is exactly what put two apps
    into production serving whatever a stale feature branch built:
    `ping-pong`'s workflow ended up wired to `[main, 'claude/**']` and `snake`'s
    to a single named branch from a finished PR — both left over from a session
    scaffolding CI mid-feature, and both meant a push to that branch redeployed
    the live app with no PR and no merge to main. On a feature branch the
    workflow looks "missing" because it correctly did not run, and that is the
    point, not a bug to work around by widening the trigger.

    `dev_app` below is the one sanctioned exception, and it is not the old
    parameter under a new name: the branch name is fixed at `dev`, it is never
    taken from the caller, and it deploys a SEPARATE app rather than the live
    one. What made `ping-pong` and `snake` dangerous was an arbitrary branch
    pushing to the production app; a declared sister app has neither half of
    that.

    Secrets: one per app, and PAAS_KEY is this app's — its scoped deploy key,
    which authenticates both halves of a deploy (the backend's /refresh call and
    the frontend upload). It can only ever deploy this one app, so a repository
    with a `dev_app` needs a second, PAAS_KEY_DEV, holding the sister app's own
    key. `required_secrets` in the result lists exactly the ones to install.

    Do not ask the user to create that secret by hand — set it yourself. The key
    is returned by apps_create (as `paas_key`) and re-issued by apps_deploy_key,
    and github_secret_set writes it to the repository. So the whole chain — create
    the app, take its key, install it as PAAS_KEY, write the workflow, push — needs
    no human step. Only a frontend-shipping app needs this at all.

    `kind` says what the repository ships — 'frontend' (a static site: zip and
    upload, no container), 'backend' (a Docker image) or 'both'. **Leave it out
    and the platform asks the repository**, keying on whether it has a
    Dockerfile. Pass it when you already know, or when the repo is one the
    platform's GitHub token cannot read.

    This matters because the two are not variations of one file. A static site
    handed the backend workflow fails at `docker build`, minutes into the first
    run, with an error about a missing Dockerfile rather than about the workflow
    being the wrong shape — which is exactly what happened on 2026-09-24. A
    frontend-only workflow has no image, no moving tag and no deploy
    verification, because the upload IS the deploy.

    `repo_name` defaults to the app id. Pass it explicitly when the GitHub
    repository is named differently from the app.

    `dev_app` — for a repository with a **dev branch**, the id of the sister app
    that `dev` deploys to. Pass it and the returned workflow serves both from one
    file: `main` deploys `app_id`, `dev` deploys `dev_app`, each to its own
    hostname, database, environment, schedule and release cadence. `dev` is added
    to the trigger branches automatically.

    Create the sister app first — `apps_create <id>-dev` with its own db_engine —
    and install ITS key as a second repository secret named **PAAS_KEY_DEV**
    (apps_create returns it; github_secret_set writes it). The main app keeps
    PAAS_KEY. Two apps means two scoped keys, and each can only deploy its own.

    The convention is `<id>` and `<id>-dev`, which gives the dev app the URL
    `<id>-dev-coolify.bogdanripa.com`. Ids are capped at 31 characters, so the
    base id must be 27 or shorter for the suffix to fit.

    **The image tags differ per branch, and that is the point.** main pushes
    `:latest`, dev pushes `:dev`, each with a per-commit tag alongside. Both apps
    auto-update from their moving tag, so if dev pushed `:latest` the box's
    hourly sweep would deploy a dev build to production — silently, because a
    deploy that succeeds looks identical whichever commit it carries. Do not
    collapse the two tags.
    """
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "SELECT id, db_engine, health_path FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app — create it first with apps_create")

    repo = repo_name or app_id
    health_path = row["health_path"] or "/"

    # The sister app must already exist, for the same reason the main one must:
    # the workflow's first /refresh is what creates its container, and a deploy
    # aimed at an app that is not registered fails at the hook with a 404 that
    # reads like an auth problem. Checked here rather than left to the run.
    if dev_app:
        if dev_app == app_id:
            raise HTTPException(422, "dev_app must be a different app from app_id")
        async with pool().acquire() as c:
            if not await c.fetchval("SELECT 1 FROM apps WHERE id = $1", dev_app):
                raise HTTPException(
                    404, f"no such app '{dev_app}' — create the sister app first "
                         f"with apps_create, then re-run this")

    branch_list = ["main", "dev"] if dev_app else ["main"]

    # What does this repository actually ship? Asked, not assumed — a static
    # site handed the backend workflow fails at `docker build` minutes into the
    # first run, and the error names a missing Dockerfile rather than a wrong
    # workflow, so nobody reading it learns what went wrong.
    detected_from = "given explicitly"
    if kind is None:
        kind, detected_from = await _detect_kind(repo)
    wants_build = None
    if kind in ("frontend", "both"):
        wants_build = await _detect_build(repo)

    notes = [
        ("Each push to main builds an arm64 image and pushes it to ghcr.io "
         f"tagged ':latest' (and with the commit sha for traceability), then "
         f"calls {app_id}'s /refresh hook; a push to dev does the same with "
         f"':dev' and {dev_app}. A push to any OTHER branch does nothing, "
         "deliberately — the trigger is main and dev, and that is not a "
         "parameter to widen. Work on a feature branch and merge it to main or "
         "dev to ship; do not repoint the trigger at the branch you are on, "
         "which is how a feature branch ends up deploying to a live app "
         "indefinitely after the feature is done (ARCHITECTURE.md has the "
         "ping-pong/snake incident)."
         if dev_app else
         "Each push to main builds an arm64 image and pushes it to ghcr.io "
         "tagged ':latest' (and with the commit sha for traceability), then "
         "calls this app's /refresh hook so the box redeploys the new image "
         "right away. A push to any other branch does nothing, deliberately — "
         "this is not a parameter to widen. Work on a feature branch and merge "
         "it to main to ship; do not repoint the trigger at the branch you are "
         "on, which is how a feature branch ends up deploying to production "
         "indefinitely after the feature is done (ARCHITECTURE.md has the "
         "ping-pong/snake incident)."),
        "The image is built natively on an arm64 runner rather than cross-built "
        "under QEMU, so build time is dominated by the image itself and not by "
        "emulation. It still varies with size and with what has to compile. Do "
        "not size a polling loop off a number quoted here — watch the run, or "
        "time the first one and use that. Note the arm64 runners are free only "
        "on public repositories; on a private repo the job waits for a runner "
        "that never comes, and the workflow comment names the fallback.",
        "The deploy is checked twice, and the two catch different things. "
        "'Wait for the deploy to be verified' polls the box's own verdict "
        "(GET /apps/<id>/refresh): the /refresh POST answers 202 the moment the "
        "deploy is queued — it cannot wait for the result, because Cloudflare "
        "cuts the request at ~100s — so this is where a rollback is caught. It "
        "is the ONLY step that can catch one: Coolify rolls a failed deploy back "
        "silently and the previous container keeps serving, so the app's health "
        "endpoint answers perfectly while running the OLD code.",
        f"'Wait for the new version to be healthy' then requests this app's "
        f"health_path ({health_path}) and requires a 2xx, which additionally "
        "catches an app that starts, reports healthy and still serves errors. If "
        "this app also ships a frontend, keep health_path on a path the BACKEND "
        "owns: '/' is served by the static bundle from the CDN with no container "
        "involved, so a '/' check passes even with a dead API.",
        ("The workflow needs TWO secrets: PAAS_KEY for this app and "
         f"PAAS_KEY_DEV for {dev_app}, each that app's own scoped deploy key. "
         "Set both with github_secret_set — apps_create returns each key as "
         "`paas_key` — rather than asking the user to paste them. Each is scoped "
         "to its own app, so a leaked copy can only deploy that one."
         if dev_app else
         "The workflow needs ONE secret: PAAS_KEY, this app's scoped deploy key. "
        "Set it yourself with github_secret_set — apps_create returns the key as "
        "`paas_key` and apps_deploy_key re-issues one — rather than asking the "
        "user to paste it. It is scoped to this app alone, so a leaked copy can "
        "only deploy this one app."),
        "The first /refresh call is what CREATES the app's container: an app is "
        "registered as a bare id, and its pipeline reports what it built. After "
        "that the box watches the tag itself and redeploys only when the digest "
        "actually changes. It also sweeps hourly, so a missed or failed /refresh "
        "call delays a deploy rather than losing it.",
        "Set the app's secrets BEFORE the first push, with apps_env_set. An app "
        "with no container yet takes them as staged and they are injected when "
        "the first container is created. This is not an optimisation: an app "
        "that reads config at import (an OpenAI client, a database driver) "
        "throws on a missing key, so without them its first container fails its "
        "healthcheck and is rolled back — leaving no container to configure and "
        "a build that will fail the same way every time.",
        "The app must already exist here before the first workflow run (create it "
        "with apps_create). New apps have auto-update on by default; toggle it "
        "with apps_autoupdate.",
        "The push authenticates with the built-in GITHUB_TOKEN, which works "
        "because the package CI creates is automatically linked to this repo. "
        "The one case where it does NOT work: if the image was ever pushed by "
        "hand first (a manual `docker push`), the ghcr package already exists "
        "owned by your account with no repo link, and CI then fails at push with "
        "'403 Forbidden'. Fix it in the package's GitHub settings → Manage "
        "Actions access → add this repo with the Write role, or let CI create the "
        "package from the start.",
    ]
    if dev_app:
        notes.insert(0,
            f"This repository deploys TWO apps: pushes to main deploy {app_id} "
            f"(image tag :latest), pushes to dev deploy {dev_app} (image tag "
            ":dev). They share a repository and a Dockerfile and nothing else — "
            "separate hostname, database, environment variables, scheduled jobs, "
            "sleep behaviour, analytics and deploy key. Release them "
            "independently; neither waits for the other, and the concurrency "
            "group is per-branch so they never serialise.")
        notes.insert(1,
            "The two image tags must never be collapsed into one. Each app "
            "auto-updates from its own moving tag, so a dev build pushed to "
            ":latest would be deployed to production by the box's hourly sweep, "
            "with nothing to notice it: a deploy that succeeds looks the same "
            "whichever commit it carries. Each branch also gets a per-commit tag "
            "(:sha-xxxxxxx on main, :dev-xxxxxxx on dev) so either side can be "
            "rolled back to a specific build.")

    if row["db_engine"]:
        notes.append(
            f"This app has a {row['db_engine']} database. Its connection string is "
            "injected as the DATABASE_URL environment variable on every deploy — "
            "read it from the environment, never hardcode it. Run migrations from "
            "the app's own startup code or via db_run_script, not from the workflow.")

    secrets = [
        {"name": "PAAS_KEY",
         "value_from": f"{app_id}'s scoped deploy key — apps_create returns it "
                       "as `paas_key`, apps_deploy_key re-issues one",
         "install_with": "github_secret_set",
         "used_by": "the main branch: both the backend /refresh call and the "
                    "frontend upload"},
    ]
    if dev_app:
        secrets.append(
            {"name": "PAAS_KEY_DEV",
             "value_from": f"{dev_app}'s OWN scoped deploy key — the sister app's "
                           "`paas_key`, not a copy of PAAS_KEY, which cannot "
                           "deploy it",
             "install_with": "github_secret_set",
             "used_by": "the dev branch: both the backend /refresh call and the "
                        "frontend upload"})

    return {
        "app_id": app_id,
        "url": app_url(app_id),
        "setup": [
            "Drive this end to end with your GitHub tools — including the "
            "secrets. No human step. Do not build the image locally.",
            "0. Install every secret in `required_secrets` in the app's repo "
            "with github_secret_set, using the key apps_create returned for "
            "EACH app (apps_deploy_key re-issues one). Do this first: the "
            "workflow's deploy step fails without it. Set each app's own "
            "secrets now too (apps_env_set) — they are staged until the first "
            "container exists, and an app that needs one at import cannot boot "
            "without it.",
            "1. Write `workflow` verbatim to `workflow_path` in the app's repo "
            "and commit it. If the repository name does not match the app id, "
            "call this tool again with `repo_name` rather than editing the file "
            "by hand — never repoint the branch trigger.",
            "2. Make sure the app's Dockerfile satisfies "
            "`dockerfile_requirements`.",
            ("3. Merge to dev to ship the dev app, to main to ship production. "
             "CI builds and pushes the arm64 image for that branch and calls "
             "that app's /refresh hook; the box redeploys the new image. "
             "Deploying is CI's job — there is no tool to deploy an app by "
             "hand, and pushing straight to a feature branch will not trigger "
             "it."
             if dev_app else
             "3. Merge to main. CI builds and pushes the arm64 image and calls "
             "the app's /refresh hook; the box redeploys the new image. "
             "Deploying is CI's job — there is no tool to deploy an app by "
             "hand, and pushing straight to a feature branch will not trigger "
             "it."),
        ],
        "workflow_path": ".github/workflows/deploy.yml",
        "kind": kind,
        "kind_detected_from": detected_from,
        "workflow": _assembled_workflow(kind, bool(wants_build), app_id, repo,
                                        branch_list, health_path, dev_app),
        # A list, not their singular "main": with a sister app this is
        # ["main", "dev"] and reporting one of them would be a lie.
        "deploys_from_branches": branch_list,
        "health_path": health_path,
        # The platform installs these itself (see the notes). One per app, so a
        # repository with a sister app needs two — one scoped key each.
        "required_secrets": secrets,
        "dockerfile_requirements": _dockerfile_rules(health_path),
        "notes": notes,
        # Two variants because a static site is not always a built site. Picking
        # the wrong one fails loudly and early (no package.json, or no build
        # script), so the choice is stated rather than defaulted.
        "optional_frontend_job": {
            "which": "Pick by whether the site has a build step. `with_build` for "
                     "Vite/CRA/Next-export and anything else with a `build` "
                     "script. `no_build` when the checked-in files ARE the site — "
                     "a game, a landing page, a status page, anything "
                     "apps_frontend_write would publish. Running the build "
                     "variant against a no-build site fails on the missing "
                     "package.json or the missing script.",
            "with_build": _frontend_job(*_fe_refs(app_id, dev_app)),
            "no_build": _frontend_job_no_build(*_fe_refs(app_id, dev_app)),
        },
        "frontend_notes": [
            "An app can have a backend (docker image), a static frontend (a zip of "
            "built assets), or both. The workflow above is the backend half; append "
            "one of `optional_frontend_job` under `jobs:` to also ship a frontend. "
            "Skip the `deploy` job entirely for a frontend-only app.",
            "Frontends are served by a shared static host behind the CDN — there is "
            "no container, no image and no cold start for them, and a deploy is just "
            "an upload (about a second).",
            "With both, they share one hostname and requests resolve with one "
            "rule, with nothing to configure: a file in the bundle is served, and "
            "everything else — every write, and every path the bundle does not "
            "contain — goes to the backend. Call your API from the frontend with a "
            "relative path: same origin, so no CORS and no API base URL.",
            "Putting the backend under '/api' is a tidy convention, but nothing "
            "enforces it: OAuth callbacks, downloads and server-rendered pages "
            "work wherever they live, because they are simply paths the bundle "
            "does not have. A path nobody has — not in the bundle, and rejected "
            "by the backend too — is a 404, and the bundle's own 404.html is "
            "used if it ships one. A single-page app whose client-side router "
            "owns those paths sets apps_update spa=true to get index.html back "
            "instead.",
            "The frontend upload needs the app's scoped deploy key as the PAAS_KEY "
            "repository secret. Install it yourself rather than asking the user: "
            "apps_create returns the key as `paas_key` (apps_deploy_key re-issues "
            "one), and github_secret_set writes it to the repo. The same key "
            "authenticates the backend's /refresh call, so one secret covers both "
            "halves of a deploy, and a leaked copy can only deploy this one app.",
        ],
    }
