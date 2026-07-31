from fastapi import APIRouter, Depends, HTTPException
from textwrap import dedent, indent

from ..auth import require_key
from ..db import pool
from ..config import app_url, DOMAIN_SUFFIX

router = APIRouter(prefix="/apps", tags=["apps"],
                   dependencies=[Depends(require_key)])


def _frontend_job(app_id: str) -> str:
    """The optional frontend half of the workflow: build static assets, zip them,
    upload. Separate job so an app can have a frontend, a backend, or both — and
    so a frontend deploy doesn't wait on an image build. Indented two spaces so it
    drops straight in under the workflow's `jobs:` key."""
    return indent(dedent(f"""\
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
              run: cd dist && zip -qr ../site.zip .

            # Needs the app's scoped deploy key (PAAS_KEY). Unlike the backend's
            # /refresh hook this uploads real content, so it is authenticated.
            # Get one with apps_deploy_key and add it as a repository secret.
            - name: Upload the frontend
              run: |
                curl -fsS -X PUT \\
                  -H "Authorization: Bearer ${{{{ secrets.PAAS_KEY }}}}" \\
                  --data-binary @site.zip \\
                  "https://api-coolify.bogdanripa.com/apps/{app_id}/frontend"
        """), "  ")


def _workflow(app_id: str, repo_name: str) -> str:
    return dedent(f"""\
        name: deploy
        on:
          push:
            branches: [main]

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
                  tags: |
                    ghcr.io/bogdanripa/{repo_name}:latest
                    ghcr.io/bogdanripa/{repo_name}:${{{{ steps.tag.outputs.value }}}}

              # The box watches :latest and redeploys when the digest moves. This
              # asks it to check now instead of waiting for the hourly sweep, and
              # authenticates with this app's scoped deploy key (PAAS_KEY), which
              # can only deploy this one app.
              - name: Trigger deploy on the Pi
                run: |
                  curl -fsS -X POST \\
                    -H "Authorization: Bearer ${{{{ secrets.PAAS_KEY }}}}" \\
                    "https://api-coolify.bogdanripa.com/apps/{app_id}/refresh"

              # /refresh only queues the deploy; poll the app until it actually
              # answers so a crash-looping container fails the run instead of
              # going green. 502/503/504/000 = proxy can't reach the container.
              - name: Wait for the new version to be healthy
                run: |
                  url="https://{app_id}{DOMAIN_SUFFIX}"
                  for i in $(seq 1 40); do
                    code=$(curl -s -o /dev/null -w '%{{http_code}}' "$url" || echo 000)
                    case "$code" in
                      502|503|504|000) echo "waiting ($code)"; sleep 6 ;;
                      *) echo "up (HTTP $code)"; exit 0 ;;
                    esac
                  done
                  echo "app did not come up — check: apps_logs {app_id}"; exit 1
        """)


DOCKERFILE_RULES = dedent("""\
    The image must satisfy these, or the container fails to start or fails its
    healthcheck and is rolled back — while the deploy call still looks like it
    succeeded, so verify with apps_logs after deploying:

    1. Built for linux/arm64 (the workflow handles this via the `platforms` key).
    2. Listen on port 80 on BOTH IP families — NOT 0.0.0.0 alone, NOT 127.0.0.1,
       and beware that ':: ' is not automatically dual-stack in every runtime.
       Two different failures come from getting this wrong, because two different
       clients connect: the healthcheck runs INSIDE the container against
       http://localhost:80/ (which resolves to ::1, IPv6 first), while the proxy
       connects from OUTSIDE to the container's IPv4 address.
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
    3. Run as root, or grant CAP_NET_BIND_SERVICE — binding port 80 is
       privileged. A `USER node`-style line makes the app die at startup with
       EACCES, which looks like any other "won't start". Drop the USER line.
    4. A HEALTHCHECK instruction so the platform can tell a started container
       from a ready one. The base image must contain curl or wget (Coolify tries
       curl, then wget) — the plain slim/alpine, scratch and distroless images do
       NOT ship curl, so install it.

    Example (note the curl install and the :: bind):

        FROM node:22-slim
        RUN apt-get update && apt-get install -y --no-install-recommends curl \\
            && rm -rf /var/lib/apt/lists/*
        WORKDIR /srv
        COPY package*.json ./
        RUN npm ci --omit=dev
        COPY . .
        ENV PORT=80
        EXPOSE 80
        # the server must listen on :: — an IPv4-only bind fails the healthcheck
        HEALTHCHECK --interval=10s --timeout=3s --start-period=10s \\
          CMD curl -fsS http://localhost:80/ || exit 1
        CMD ["node", "server.js"]
    """)


@router.get("/{app_id}/deploy-workflow", operation_id="apps_deploy_workflow",
            summary="Get the GitHub Actions workflow that redeploys this app on every push")
async def deploy_workflow(app_id: str, repo_name: str | None = None):
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

    Secrets: a **backend** deploy needs none — CI pushes the image and calls the
    app's unauthenticated /refresh hook. A **frontend** upload does, because it
    sends real content to be served on the app's domain, so it authenticates with
    that app's scoped deploy key as the PAAS_KEY repository secret.

    Do not ask the user to create that secret by hand — set it yourself. The key
    is returned by apps_create (as `paas_key`) and re-issued by apps_deploy_key,
    and github_secret_set writes it to the repository. So the whole chain — create
    the app, take its key, install it as PAAS_KEY, write the workflow, push — needs
    no human step. Only a frontend-shipping app needs this at all.

    `repo_name` defaults to the app id. Pass it explicitly when the GitHub
    repository is named differently from the app.
    """
    async with pool().acquire() as c:
        row = await c.fetchrow("SELECT id, db_engine FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app — create it first with apps_create")

    repo = repo_name or app_id

    notes = [
        "Each push to main builds an arm64 image and pushes it to ghcr.io tagged "
        "':latest' (and with the commit sha for traceability), then calls this "
        "app's /refresh hook so the box redeploys the new image right away.",
        "The workflow needs ONE secret: PAAS_KEY, this app's scoped deploy key. "
        "Set it yourself with github_secret_set — apps_create returns the key as "
        "`paas_key` and apps_deploy_key re-issues one — rather than asking the "
        "user to paste it. It is scoped to this app alone, so a leaked copy can "
        "only redeploy this app to an image that is already in the registry.",
        "The box never takes an image from the caller: /refresh only makes it "
        "re-check the tag it already watches, and it redeploys solely when that "
        "tag's digest actually changes. It also sweeps hourly, so a missed or "
        "failed /refresh call delays a deploy rather than losing it.",
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
            "and commit it.",
            "2. Make sure the app's Dockerfile satisfies "
            "`dockerfile_requirements`.",
            "3. Push to main. CI builds and pushes the arm64 image and calls the "
            "app's /refresh hook; the box redeploys the new image. Deploying is "
            "CI's job — there is no tool to deploy an app by hand.",
        ],
        "workflow_path": ".github/workflows/deploy.yml",
        "workflow": _workflow(app_id, repo),
        # One secret, and the platform installs it itself (see the notes).
        "required_secrets": [
            {"name": "PAAS_KEY",
             "value_from": "the app's scoped deploy key — apps_create returns it "
                           "as `paas_key`, apps_deploy_key re-issues one",
             "install_with": "github_secret_set",
             "used_by": "both the backend /refresh call and the frontend upload"},
        ],
        "dockerfile_requirements": DOCKERFILE_RULES,
        "notes": notes,
        "optional_frontend_job": _frontend_job(app_id),
        "frontend_notes": [
            "An app can have a backend (docker image), a static frontend (a zip of "
            "built assets), or both. The workflow above is the backend half; append "
            "`optional_frontend_job` under `jobs:` to also ship a frontend. Skip the "
            "`deploy` job entirely for a frontend-only app.",
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
            "does not have. A client-side route the backend also rejects falls "
            "back to index.html, so SPA deep links work too.",
            "The frontend upload needs the app's scoped deploy key as the PAAS_KEY "
            "repository secret. Install it yourself rather than asking the user: "
            "apps_create returns the key as `paas_key` (apps_deploy_key re-issues "
            "one), and github_secret_set writes it to the repo. A leaked copy can "
            "only deploy this one app. Only the backend deploy is secretless.",
        ],
    }
