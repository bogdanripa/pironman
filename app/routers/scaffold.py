from fastapi import APIRouter, Depends, HTTPException
from textwrap import dedent

from ..auth import require_key
from ..db import pool
from ..config import app_url, DOMAIN_SUFFIX

router = APIRouter(prefix="/apps", tags=["apps"],
                   dependencies=[Depends(require_key)])


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

              - name: Redeploy on the Pi
                run: |
                  curl -fsS -X PUT \\
                    "https://api-coolify.bogdanripa.com/apps/{app_id}/code" \\
                    -H "Authorization: Bearer ${{{{ secrets.PAAS_KEY }}}}" \\
                    -H 'Content-Type: application/json' \\
                    -d '{{"image":"ghcr.io/bogdanripa/{repo_name}:${{{{ steps.tag.outputs.value }}}}"}}'
        """)


DOCKERFILE_RULES = dedent("""\
    The image must satisfy three things or the deploy will fail:

    1. Built for linux/arm64 (the workflow handles this via the `platforms` key).
    2. The server listens on 0.0.0.0:80 — not 3000, not 8080. Bind to 0.0.0.0,
       not 127.0.0.1, or the proxy cannot reach it from outside the container.
    3. A HEALTHCHECK instruction, so the platform can tell a started container
       from a ready one. Needed for zero-downtime redeploys and required if the
       app sleeps when idle. The base image must contain curl or wget for this
       to work — scratch and distroless images cannot run one.

    Example:

        FROM node:22-slim
        WORKDIR /srv
        COPY package*.json ./
        RUN npm ci --omit=dev
        COPY . .
        ENV PORT=80
        EXPOSE 80
        HEALTHCHECK --interval=10s --timeout=3s --start-period=10s \\
          CMD curl -fsS http://localhost:80/health || exit 1
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

    The user must create the PAAS_KEY repository secret themselves; its value is
    an API key for this platform, which cannot be issued over the API.

    `repo_name` defaults to the app id. Pass it explicitly when the GitHub
    repository is named differently from the app.
    """
    async with pool().acquire() as c:
        row = await c.fetchrow("SELECT id, db_engine FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app — create it first with apps_create")

    repo = repo_name or app_id

    notes = [
        "Each push to main builds an arm64 image, pushes it to ghcr.io tagged "
        "with the commit sha, and calls this platform to redeploy that exact tag.",
        "The workflow deploys an immutable sha- tag rather than :latest, because "
        "re-pushing :latest does not reliably force a fresh pull.",
        "The app must already exist here before the first workflow run; CI only "
        "ever updates an existing app.",
        "The push authenticates with the built-in GITHUB_TOKEN, which works "
        "because the package CI creates is automatically linked to this repo. "
        "The one case where it does NOT work: if the image was ever pushed by "
        "hand first (a manual `docker push` during setup), the ghcr package "
        "already exists owned by your account with no repo link, and CI then "
        "fails at push with '403 Forbidden'. Fix it in the package's GitHub "
        "settings → Manage Actions access → add this repo with the Write role; "
        "or delete the hand-pushed package and let the first CI run create it. "
        "Letting CI create the package from the start avoids this entirely.",
        "If the package is private, the Pi needs a pull credential for it — it "
        "has one, but a brand-new private package may need to be linked to its "
        "repository in the package's GitHub settings before the Pi can pull.",
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
            "Drive this yourself with your GitHub tools; only the secret may need "
            "a human. Do not build the image locally.",
            "1. Write `workflow` verbatim to `workflow_path` in the app's repo "
            "and commit it.",
            "2. Create the repository secret in `required_secrets` (PAAS_KEY). "
            "Its value is the app's deploy key — the `paas_key` apps_create "
            "returned, or call apps_deploy_key to issue a fresh one. If you "
            "cannot set repo secrets programmatically, ask the user to add it "
            "once.",
            "3. Make sure the app's Dockerfile satisfies "
            "`dockerfile_requirements`.",
            "4. Push to main. CI builds and pushes the arm64 image, then calls "
            "apps_update_code to redeploy.",
            "First deploy shortcut: if PAAS_KEY is not set yet, the build/push "
            "still runs; once the image is in ghcr, call apps_update_code with "
            "that tag yourself to get the app live now.",
        ],
        "workflow_path": ".github/workflows/deploy.yml",
        "workflow": _workflow(app_id, repo),
        "required_secrets": [{
            "name": "PAAS_KEY",
            "where": "GitHub repository → Settings → Secrets and variables → Actions",
            "value": "This app's deploy key, scoped so it can only redeploy this "
                     "app. apps_create returned it as `paas_key` when the app was "
                     "created; if you don't have it, call apps_deploy_key to issue "
                     "a fresh one (that revokes the previous). Shown once — paste "
                     "it into the PAAS_KEY secret.",
        }],
        "dockerfile_requirements": DOCKERFILE_RULES,
        "notes": notes,
    }
