import os

_REQUIRED = ["COOLIFY_TOKEN", "COOLIFY_PROJECT", "COOLIFY_SERVER",
             "COOLIFY_DESTINATION", "PAAS_DB_PASSWORD"]
_missing = [k for k in _REQUIRED if not os.environ.get(k)]
if _missing:
    raise SystemExit("missing required environment variables: " + ", ".join(_missing))

COOLIFY_URL         = os.environ.get("COOLIFY_URL", "http://coolify:8000")
COOLIFY_TOKEN       = os.environ["COOLIFY_TOKEN"]
COOLIFY_PROJECT     = os.environ["COOLIFY_PROJECT"]
COOLIFY_SERVER      = os.environ["COOLIFY_SERVER"]
COOLIFY_DESTINATION = os.environ["COOLIFY_DESTINATION"]
COOLIFY_ENV_NAME    = os.environ.get("COOLIFY_ENV_NAME", "production")

DOMAIN_SUFFIX = os.environ.get("DOMAIN_SUFFIX", "-coolify.bogdanripa.com")

# ghcr read credential for the auto-updater's digest checks (docker pull of
# private packages). Optional — without it auto-update still works for public
# images. This is ONE server-side credential for the whole platform, not a
# per-repo deploy secret, so it does not reintroduce per-app secrets.
GHCR_USER  = os.environ.get("GHCR_USER", "")
GHCR_TOKEN = os.environ.get("GHCR_TOKEN", "")

# GitHub API token for the github_secret_* tools (repository Actions secrets).
# Needs `repo` scope (classic PAT) or a fine-grained token with Secrets: write.
# Falls back to GHCR_TOKEN, which only works if that PAT also carries repo scope.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or GHCR_TOKEN

def _pg_host() -> str:
    """Resolve the Postgres container name live. Coolify recreates database
    resources on routine config edits and the name changes each time, so a
    hardcoded host would strand the API from its own registry."""
    h = os.environ.get("PAAS_DB_HOST")
    if h:
        return h
    import subprocess
    return subprocess.run(["/usr/local/bin/pdb", "host", "--engine", "postgres"],
                          capture_output=True, text=True, check=True).stdout.strip()


PAAS_DB = dict(
    host=_pg_host(),
    port=int(os.environ.get("PAAS_DB_PORT", 5432)),
    user=os.environ.get("PAAS_DB_USER", "_paas"),
    password=os.environ["PAAS_DB_PASSWORD"],
    database=os.environ.get("PAAS_DB_NAME", "_paas"),
)

def app_url(app_id: str) -> str:
    return f"https://{app_id}{DOMAIN_SUFFIX}"

def app_fqdn(app_id: str) -> str:
    # Coolify stores the ORIGIN scheme. http:// — Cloudflare terminates TLS at
    # the edge; https:// here would make Traefik redirect and loop.
    return f"http://{app_id}{DOMAIN_SUFFIX}"
