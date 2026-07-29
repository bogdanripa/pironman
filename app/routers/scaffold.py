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

              # No secret: the box watches :latest and redeploys the new image.
              # This just asks it to do so now instead of at the next hourly poll.
              - name: Trigger deploy on the Pi
                run: |
                  curl -fsS -X POST \\
                    "https://api-coolify.bogdanripa.com/apps/{app_id}/refresh"
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
        "Each push to main builds an arm64 image and pushes it to ghcr.io tagged "
        "':latest' (and with the commit sha for traceability), then calls this "
        "app's /refresh hook so the box redeploys the new image right away.",
        "No PAAS_KEY, no deploy secret: the box auto-updates. It watches the "
        "app's ':latest' tag — hourly, and immediately when /refresh is called — "
        "and redeploys only when the image digest actually changes. The /refresh "
        "call is unauthenticated on purpose; it takes no image, so a caller can "
        "at most trigger that check.",
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
            "Drive this end to end with your GitHub tools — no secret, no human "
            "step. Do not build the image locally.",
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
        "required_secrets": [],  # none — the box auto-updates, no deploy secret
        "dockerfile_requirements": DOCKERFILE_RULES,
        "notes": notes,
    }
