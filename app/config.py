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

# Analytics: paas-api tails the shared Traefik access log to build cross-app
# visitor stats (see app/analytics.py). ANALYTICS_PROXY is the docker container
# name of the edge proxy whose logs are read; ANALYTICS_SALT keys the cookieless
# visitor hash (change it and past visitors count as new — treat it as stable).
ANALYTICS_PROXY = os.environ.get("ANALYTICS_PROXY", "coolify-proxy")
ANALYTICS_SALT  = os.environ.get("ANALYTICS_SALT", "pironman-analytics-v1")

# Telegram alerting (app/alerts.py). Both must be set for alerts to fire; unset,
# the monitor loop is a no-op. TELEGRAM_BOT_TOKEN from @BotFather; TELEGRAM_CHAT_ID
# is the chat/DM to notify. ALERT_5XX_THRESHOLD is how many new 5xx responses in a
# monitor interval trip a server-error alert.
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
ALERT_5XX_THRESHOLD = int(os.environ.get("ALERT_5XX_THRESHOLD", "5"))

# Sablier scale-to-zero enrollment (app/sablier.py). paas-api stamps the Sablier
# Traefik-plugin middleware onto each app's Coolify custom labels so idle apps
# sleep and the enrollment survives redeploys. SABLIER_URL is how Traefik reaches
# the Sablier container; the rest are the plugin middleware's parameters.
#   SABLIER_EXCLUDE — apps that must never sleep (the control plane itself).
#   SABLIER_AUTO_ENROLL — when true, new/updated apps are enrolled automatically;
#     keep it false until enrollment is verified on one app (apps_sablier).
SABLIER_URL              = os.environ.get("SABLIER_URL", "http://sablier:10000")
SABLIER_SESSION_DURATION = os.environ.get("SABLIER_SESSION_DURATION", "5m")
# "blocking" holds the request until the app is up and then returns the app's own
# response — a caller just sees a slow first request. "dynamic" instead answers
# with Sablier's HTML waiting page, which is wrong for anything a program calls
# (an API client or MCP server gets HTML where it expected JSON), so blocking is
# the default here. SABLIER_BLOCKING_TIMEOUT bounds the wait.
SABLIER_STRATEGY         = os.environ.get("SABLIER_STRATEGY", "blocking")
SABLIER_BLOCKING_TIMEOUT = os.environ.get("SABLIER_BLOCKING_TIMEOUT", "60s")
SABLIER_EXCLUDE          = set(
    x.strip() for x in os.environ.get("SABLIER_EXCLUDE", "api").split(",") if x.strip())

# The shared static host's app id. It fronts every app that sleeps — it is what
# wakes them — so it must never sleep itself, and that is not left to
# SABLIER_EXCLUDE being spelled correctly: app/sablier.py excludes it outright.
STATIC_HOST_APP = os.environ.get("STATIC_HOST_APP", "web")
SABLIER_AUTO_ENROLL      = os.environ.get("SABLIER_AUTO_ENROLL", "").lower() in (
    "1", "true", "yes")

# The registry namespace apps are built into, used when scaffolding a workflow.
# The platform never derives an app's image itself: which image an app runs is
# reported by its pipeline when it deploys.
GHCR_OWNER = os.environ.get("GHCR_OWNER", "bogdanripa")



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

# Cloudflare cache invalidation (app/cdn.py). Both must be set for a frontend
# deploy to purge the URLs it just published; without them the platform still
# works, but unhashed assets stay cached at the edge until their TTL expires.
# The token needs the Zone.Cache Purge permission on this zone only.
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_ZONE_ID   = os.environ.get("CLOUDFLARE_ZONE_ID", "")

# This control plane's own app id. It cannot verify its own deploy — the request
# is served by the container being replaced — so that one path skips the check.
CONTROL_PLANE_APP = os.environ.get("CONTROL_PLANE_APP", "api")

# host_run_script (app/hostexec.py) borrows `nsenter` from a throwaway container
# to reach the host's namespaces. Any image carrying nsenter will do; unset, it
# uses this control plane's own image, which is already on the box — so there is
# normally nothing to set here.
HOST_EXEC_IMAGE = os.environ.get("HOST_EXEC_IMAGE", "")

# The dashboard runs as its own app, so the control plane must accept its origin
# for cross-origin reads (see app/cors.py).
DASHBOARD_ORIGIN = os.environ.get(
    "DASHBOARD_ORIGIN", f"https://dashboard{DOMAIN_SUFFIX}")


def app_url(app_id: str) -> str:
    return f"https://{app_id}{DOMAIN_SUFFIX}"

def app_fqdn(app_id: str) -> str:
    # Coolify stores the ORIGIN scheme. http:// — Cloudflare terminates TLS at
    # the edge; https:// here would make Traefik redirect and loop.
    return f"http://{app_id}{DOMAIN_SUFFIX}"
