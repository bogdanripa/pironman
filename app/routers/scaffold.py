from fastapi import APIRouter, Depends, HTTPException
from textwrap import dedent, indent

from ..auth import require_key
from ..db import pool
from ..config import app_url, DOMAIN_SUFFIX, GHCR_OWNER

router = APIRouter(prefix="/apps", tags=["apps"],
                   dependencies=[Depends(require_key)])


def _upload_step(app_id: str) -> str:
    """The last step of either frontend job. Authenticated with PAAS_KEY, which
    this platform installs itself — unlike the backend's /refresh hook this
    uploads real content, so it cannot be an unauthenticated call."""
    return dedent(f"""\
        # PAAS_KEY is this app's scoped deploy key. It is already in the repo:
        # apps_create returns it and github_secret_set installs it. Nothing for
        # a human to paste.
        - name: Upload the frontend
          run: |
            curl -fsS -X PUT \\
              -H "Authorization: Bearer ${{{{ secrets.PAAS_KEY }}}}" \\
              --data-binary @site.zip \\
              "https://api-coolify.bogdanripa.com/apps/{app_id}/frontend"
        """)


def _frontend_job(app_id: str) -> str:
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

        """) + indent(_upload_step(app_id), "    "), "  ")


def _frontend_job_no_build(app_id: str) -> str:
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

        """) + indent(_upload_step(app_id), "    "), "  ")


def _workflow(app_id: str, repo_name: str, branches: list[str],
              health_path: str) -> str:
    branch_list = ", ".join(branches)
    return dedent(f"""\
        name: deploy
        on:
          push:
            branches: [{branch_list}]

        # Every run pushes the same :latest tag and the box deploys whatever that
        # points at, so two overlapping runs race and the one that FINISHES last
        # wins regardless of which commit is newer. Build times vary by an order
        # of magnitude — a small node/alpine image is a minute or two, while a
        # large image or one that compiles native dependencies can take 15+
        # minutes under QEMU emulation — so a quick follow-up commit can easily
        # land first and then be undone by its predecessor. Don't build a polling
        # timeout around either figure; time your own first run and use that.
        # One run at a time, newest wins.
        concurrency:
          group: deploy-${{{{ github.ref }}}}
          cancel-in-progress: true

        jobs:
          deploy:
            runs-on: ubuntu-latest
            permissions:
              contents: read
              packages: write
            steps:
              - uses: actions/checkout@v4

              - uses: docker/setup-qemu-action@v3
              - uses: docker/setup-buildx-action@v3

              - uses: docker/login-action@v3
                with:
                  registry: ghcr.io
                  username: ${{{{ github.actor }}}}
                  password: ${{{{ secrets.GITHUB_TOKEN }}}}

              - id: tag
                run: echo "value=sha-${{GITHUB_SHA::7}}" >> $GITHUB_OUTPUT

              - uses: docker/build-push-action@v6
                with:
                  platforms: linux/arm64
                  push: true
                  # So the image can report which commit it is running. A health
                  # endpoint that answers "unknown" cannot tell you whether a
                  # deploy actually landed, which is exactly when you want to
                  # know. Ignored harmlessly if the Dockerfile declares no such
                  # ARG.
                  build-args: |
                    GIT_SHA=${{{{ github.sha }}}}
                  tags: |
                    ghcr.io/{GHCR_OWNER}/{repo_name}:latest
                    ghcr.io/{GHCR_OWNER}/{repo_name}:${{{{ steps.tag.outputs.value }}}}

              # Tells the box what this build pushed. On the app's FIRST deploy
              # that image is what creates its container — the app was registered
              # as a bare id, because only this pipeline knows what it runs. After
              # that the box watches the tag itself and this just asks it to check
              # now rather than at the next hourly sweep. Authenticated with this
              # app's scoped deploy key (PAAS_KEY), which can only deploy this one
              # app. A 2xx means the new container came up healthy — the platform
              # verifies that before answering, so a rolled-back deploy fails this
              # step instead of going green. The control plane redeploys itself
              # often and answers 404 while it restarts, so transient codes are
              # retried rather than believed.
              - name: Trigger deploy on the Pi
                run: |
                  for i in $(seq 1 10); do
                    code=$(curl -sS -o /tmp/out -w '%{{http_code}}' -X POST \\
                      -H "Authorization: Bearer ${{{{ secrets.PAAS_KEY }}}}" \\
                      -H "Content-Type: application/json" \\
                      -d '{{"image": "ghcr.io/{GHCR_OWNER}/{repo_name}:latest"}}' \\
                      "https://api-coolify.bogdanripa.com/apps/{app_id}/refresh" || echo 000)
                    case "$code" in
                      2*) cat /tmp/out; echo; exit 0 ;;
                      404|000|503|504) echo "attempt $i: $code, retrying"; sleep 10 ;;
                      401) echo "PAAS_KEY is not this app's deploy key"; exit 1 ;;
                      502) cat /tmp/out; echo
                           echo "deploy rolled back — the new container never became healthy"
                           echo "check: apps_logs {app_id}"; exit 1 ;;
                      *)  cat /tmp/out; echo; echo "refresh failed ($code)"; exit 1 ;;
                    esac
                  done
                  echo "gave up after 10 attempts"; exit 1

              # /refresh verifies the deploy server-side and returns non-2xx if
              # the container failed and Coolify rolled it back, so this step is a
              # second opinion: it also catches an app that starts and answers
              # healthily but serves errors.
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
                  url="https://{app_id}{DOMAIN_SUFFIX}{health_path}"
                  for i in $(seq 1 40); do
                    code=$(curl -s -o /dev/null -w '%{{http_code}}' "$url" || echo 000)
                    case "$code" in
                      2*) echo "up (HTTP $code)"; exit 0 ;;
                      # 404 is retried, not failed: during rollover the request can
                      # still reach the outgoing container.
                      000|404|502|503|504) echo "waiting ($code)"; sleep 6 ;;
                      *) echo "$url answered HTTP $code — the health path must "
                         echo "return 2xx without authentication"; exit 1 ;;
                    esac
                  done
                  echo "no 2xx from $url — either the app did not come up"
                  echo "(check: apps_logs {app_id}) or the app does not serve"
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


@router.get("/{app_id}/deploy-workflow", operation_id="apps_deploy_workflow",
            summary="Get the GitHub Actions workflow that redeploys this app on every push")
async def deploy_workflow(app_id: str, repo_name: str | None = None,
                          branches: str | None = None):
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
    editing the result.** Everything that legitimately varies per repository is a
    parameter here: `branches` for the branch to deploy from, `repo_name` for a
    repository named differently from the app. Hand-editing the generated file is
    how the parts that must not change get changed by accident.

    `branches` — comma-separated branch names whose pushes deploy, e.g.
    'develop' or 'main,staging'. Defaults to 'main'. **Pass the branch actually
    being worked on**: on a feature branch, a workflow wired to main builds
    nothing, and the run that looks missing is simply a workflow that never
    triggered.

    Secrets: one, PAAS_KEY — the app's scoped deploy key, which authenticates
    both halves of a deploy (the backend's /refresh call and the frontend
    upload). It can only ever deploy this one app.

    Do not ask the user to create that secret by hand — set it yourself. The key
    is returned by apps_create (as `paas_key`) and re-issued by apps_deploy_key,
    and github_secret_set writes it to the repository. So the whole chain — create
    the app, take its key, install it as PAAS_KEY, write the workflow, push — needs
    no human step. Only a frontend-shipping app needs this at all.

    `repo_name` defaults to the app id. Pass it explicitly when the GitHub
    repository is named differently from the app.
    """
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "SELECT id, db_engine, health_path FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app — create it first with apps_create")

    repo = repo_name or app_id
    branch_list = [b.strip() for b in (branches or "main").split(",") if b.strip()]
    if not branch_list:
        raise HTTPException(422, "branches must name at least one branch")
    health_path = row["health_path"] or "/"

    notes = [
        f"Each push to {' or '.join(branch_list)} builds an arm64 image and pushes "
        "it to ghcr.io tagged ':latest' (and with the commit sha for "
        "traceability), then calls this app's /refresh hook so the box redeploys "
        "the new image right away. A push to any other branch does nothing — "
        "re-run apps_deploy_workflow with `branches` to change that rather than "
        "editing the file.",
        f"Build time is dominated by the arm64 image build under QEMU emulation, "
        "and it varies enormously: a small node/alpine image finishes in a minute "
        "or two, an image with native dependencies to compile can take 15+ "
        "minutes. Do not size a polling loop off either number — watch the run, "
        "or time the first one and use that.",
        f"The 'wait for healthy' step requests this app's health_path "
        f"({health_path}) and requires a 2xx, so a green run means the container "
        "answered. If this app also ships a frontend, keep health_path on a path "
        "the BACKEND owns: '/' is served by the static bundle from the CDN with "
        "no container involved, so a '/' check passes even with a dead API.",
        "The workflow needs ONE secret: PAAS_KEY, this app's scoped deploy key. "
        "Set it yourself with github_secret_set — apps_create returns the key as "
        "`paas_key` and apps_deploy_key re-issues one — rather than asking the "
        "user to paste it. It is scoped to this app alone, so a leaked copy can "
        "only deploy this one app.",
        "The first /refresh call is what CREATES the app's container: an app is "
        "registered as a bare id, and its pipeline reports what it built. After "
        "that the box watches the tag itself and redeploys only when the digest "
        "actually changes. It also sweeps hourly, so a missed or failed /refresh "
        "call delays a deploy rather than losing it.",
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
    if row["db_engine"]:
        notes.append(
            f"This app has a {row['db_engine']} database. Its connection string is "
            "injected as the DATABASE_URL environment variable on every deploy — "
            "read it from the environment, never hardcode it. Run migrations from "
            "the app's own startup code or via db_run_script, not from the workflow.")

    return {
        "app_id": app_id,
        "url": app_url(app_id),
        "setup": [
            "Drive this end to end with your GitHub tools — including the secret. "
            "No human step. Do not build the image locally.",
            "0. Install PAAS_KEY in the app's repo with github_secret_set, using "
            "the key from apps_create (or apps_deploy_key). Do this first: the "
            "workflow's deploy step fails without it.",
            "1. Write `workflow` verbatim to `workflow_path` in the app's repo "
            "and commit it. If something about it does not fit — the branch, the "
            "repository name — call this tool again with that parameter rather "
            "than editing the file by hand.",
            "2. Make sure the app's Dockerfile satisfies "
            "`dockerfile_requirements`.",
            f"3. Push to {' or '.join(branch_list)}. CI builds and pushes the "
            "arm64 image and calls the app's /refresh hook; the box redeploys the "
            "new image. Deploying is CI's job — there is no tool to deploy an app "
            "by hand.",
        ],
        "workflow_path": ".github/workflows/deploy.yml",
        "workflow": _workflow(app_id, repo, branch_list, health_path),
        "deploys_from_branches": branch_list,
        "health_path": health_path,
        # One secret, and the platform installs it itself (see the notes).
        "required_secrets": [
            {"name": "PAAS_KEY",
             "value_from": "the app's scoped deploy key — apps_create returns it "
                           "as `paas_key`, apps_deploy_key re-issues one",
             "install_with": "github_secret_set",
             "used_by": "both the backend /refresh call and the frontend upload"},
        ],
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
            "with_build": _frontend_job(app_id),
            "no_build": _frontend_job_no_build(app_id),
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
