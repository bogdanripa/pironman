import os

COOLIFY_URL         = os.environ.get("COOLIFY_URL", "http://coolify:8000")
COOLIFY_TOKEN       = os.environ["COOLIFY_TOKEN"]
COOLIFY_PROJECT     = os.environ["COOLIFY_PROJECT"]
COOLIFY_SERVER      = os.environ["COOLIFY_SERVER"]
COOLIFY_DESTINATION = os.environ["COOLIFY_DESTINATION"]
COOLIFY_ENV_NAME    = os.environ.get("COOLIFY_ENV_NAME", "production")

DOMAIN_SUFFIX = os.environ.get("DOMAIN_SUFFIX", "-coolify.bogdanripa.com")

PAAS_DB = dict(
    host=os.environ["PAAS_DB_HOST"],
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
