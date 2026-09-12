"""Custom domains — an app's own hostname, alongside the one the platform
generates.

Every app always answers on `<id><DOMAIN_SUFFIX>`; that one is free, immediate
and cannot be taken away. A custom domain is added *next to* it, never instead of
it, because the generated host is what CI, the cron dispatcher, the wake
handshake and every internal caller use, and a rename would break all of them
silently.

Three things have to agree for a custom domain to work, and this module is what
keeps them in step:

  * the static host's router (`fe-<id>`) must list the hostname, or a request for
    it reaches Traefik's catchall and 503s — which is also exactly what a
    *sleeping* app would do, since the static host is what holds its route while
    its container is stopped;
  * the backend's own router must list it too, or the static host forwards the
    request with its marker header, nothing matches, and it comes straight back
    as "backend has no route";
  * the static host must be able to resolve the app id FROM that hostname. It
    does that for a generated host by stripping the suffix; for a custom one it
    reads the map paas-api writes beside the bundles.

DNS is the one step the platform cannot do. `*.bogdanripa.com` is a wildcard, so
a generated host needs no record — a custom domain is somebody else's zone and
needs a CNAME or A record pointing at this box, created by whoever owns it.
"""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_key
from ..config import DOMAIN_SUFFIX, app_hosts
from ..db import pool
from .. import routing, sablier

router = APIRouter(prefix="/apps", tags=["domains"],
                   dependencies=[Depends(require_key)])

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class Domain(BaseModel):
    domain: str = Field(
        description="The hostname to add, e.g. 'shop.example.com' or "
                    "'example.com'. No scheme, no path, no port — just the host. "
                    "It must be a name you control: point it at this box with a "
                    "DNS record first (see the tool description), because "
                    "nothing here can create one for you.")


def normalise(domain: str) -> str:
    """The canonical form of a hostname, or an HTTP 422 explaining why it is not
    one.

    Lowercased and stripped of a trailing dot so that one name has exactly one
    spelling: the Host header arrives lowercased in practice but is defined
    case-insensitively, and `example.com.` and `example.com` are the same name to
    DNS. Two spellings of one domain would each get a router, the duplicate check
    would not see them as duplicates, and analytics would attribute the traffic
    twice.
    """
    d = (domain or "").strip().rstrip(".").lower()
    for bad, why in (("://", "a scheme"), ("/", "a path"), (":", "a port"),
                     (" ", "a space"), ("*", "a wildcard")):
        if bad in d:
            raise HTTPException(422, f"{domain!r} contains {why} — give the "
                                     f"hostname alone, e.g. 'shop.example.com'")
    if not d:
        raise HTTPException(422, "domain is empty")
    if not d.isascii():
        # An internationalised name has to be stored in the form Traefik matches
        # and the browser sends, which is the punycode one.
        try:
            d = d.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            raise HTTPException(
                422, f"{domain!r} is not a valid internationalised domain — "
                     f"supply its punycode form (xn--…) instead")
    if len(d) > 253:
        raise HTTPException(422, f"{d!r} is longer than 253 characters")
    labels = d.split(".")
    if len(labels) < 2:
        raise HTTPException(
            422, f"{d!r} is a single label — a custom domain needs a real "
                 f"registered name like 'example.com'")
    for part in labels:
        if not _LABEL.match(part):
            raise HTTPException(
                422, f"{d!r}: '{part}' is not a valid DNS label (letters, digits "
                     f"and hyphens; not starting or ending with a hyphen)")
    if labels[-1].isdigit():
        raise HTTPException(422, f"{d!r} looks like an IP address, not a name")

    # The one rule that is about this platform rather than about DNS, and the
    # reason it cannot be left to "well, nobody would do that". Every resolver
    # here — the static host's _app_id, analytics.resolve_app — checks the custom
    # map BEFORE stripping the suffix, so a custom domain of
    # `<other-app><DOMAIN_SUFFIX>` would capture another app's routing and its
    # traffic figures at once. It is also unnecessary: those hostnames are handed
    # out by the platform, so there is nothing here for a legitimate caller.
    if d.endswith(DOMAIN_SUFFIX):
        raise HTTPException(
            422, f"{d!r} is a platform-generated hostname — every app already "
                 f"has one of those, and registering another app's would take "
                 f"over its routing. Use a domain you own.")
    return d


async def _row(c, app_id: str):
    row = await c.fetchrow(
        "SELECT id, coolify_uuid, has_frontend, redirects, sleep_when_idle, "
        "internal, custom_domains FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app")
    return row


async def _apply(c, row, domains: list[str]) -> dict:
    """Write the app's domain list and make routing match it.

    `sync_frontend_routes` covers the static host's router, the host map it reads
    and — for a fronted app — the backend's own labels. An app the static host
    does NOT front is deliberately outside that pass (it is only revisited when
    it still carries a stale marker), so its labels are written here instead.
    Without this an app with a container, no frontend and no sleeping would take
    a custom domain, report success, and answer nothing on it.
    """
    app_id = row["id"]
    await c.execute("UPDATE apps SET custom_domains = $1 WHERE id = $2",
                    domains, app_id)
    out: dict = {"id": app_id, "custom_domains": domains,
                 "urls": [f"https://{h}" for h in app_hosts(app_id, domains)]}
    try:
        out["routing"] = await routing.sync_frontend_routes(c)
    except Exception as e:
        out["routing"] = {"error": str(e)}
    fresh = await _row(c, app_id)
    if row["coolify_uuid"] and not routing.is_fronted(fresh):
        try:
            out["backend_relabelled"] = await routing.apply_backend_labels(
                app_id, row["coolify_uuid"],
                sleeps=bool(fresh["sleep_when_idle"]), fronted=False)
        except sablier.NoContainer:
            out["backend_relabelled"] = False  # never deployed; nothing to label
        except Exception as e:
            out["backend_relabelled"] = {"error": str(e)}
    return out


DNS_NOTE = (
    "DNS is yours to create and is the only step this platform cannot do for "
    "you. Point the name at this box with an **A record to its public IP**, "
    "proxied — for a subdomain as much as for an apex. "
    "Do NOT CNAME it to <something>{suffix}: those hostnames are themselves "
    "proxied by Cloudflare, so a proxied CNAME to one never reaches an origin at "
    "all. The request is handed to the bogdanripa.com edge still carrying your "
    "hostname, that zone does not serve it, and Cloudflare answers 404 from "
    "inside — with nothing in this box's access log to show for it. Measured "
    "2026-09-12 on gepetel.com. "
    "TLS is terminated by Cloudflare, not here: the origin serves plain HTTP on "
    "port 80 and has no certificate (port 443 answers with Traefik's default "
    "self-signed one), so a proxied domain must have SSL mode 'Flexible' — "
    "'Full' fails on https:// while http:// works. A domain pointed straight at "
    "the box with no proxy in front is http://-only."
).format(suffix=DOMAIN_SUFFIX)


@router.get("/{app_id}/domains", operation_id="apps_domains_list",
            summary="List the hostnames an app answers on")
async def list_domains(app_id: str):
    """Every hostname this app serves: the generated one it always has, plus any
    custom domains added with apps_domain_add."""
    async with pool().acquire() as c:
        row = await _row(c, app_id)
    custom = list(row["custom_domains"] or [])
    return {"id": app_id, "custom_domains": custom,
            "generated": f"{app_id}{DOMAIN_SUFFIX}",
            "urls": [f"https://{h}" for h in app_hosts(app_id, custom)],
            "dns": DNS_NOTE}


@router.post("/{app_id}/domains", status_code=201, operation_id="apps_domain_add",
             summary="Give an app a custom domain, alongside its generated one")
async def add_domain(app_id: str, body: Domain):
    """Add a hostname the app answers on, in addition to
    https://<id>-coolify.bogdanripa.com — which it keeps, and which everything
    internal (CI, crons, the wake handshake) goes on using.

    Routing is updated immediately and may redeploy the static host and the app's
    own container, since both carry the hostname in their Traefik rules. Traffic
    on the custom domain is attributed to this app in analytics like any other.

    Create the DNS record FIRST, or the first request will not reach the box at
    all and the failure will look like a platform one. See `dns` in the response
    and in apps_domains_list.
    """
    d = normalise(body.domain)
    async with pool().acquire() as c:
        row = await _row(c, app_id)
        if row["internal"]:
            raise HTTPException(
                400, f"{app_id} is an internal service — it has no public "
                     f"hostname at all, by design, and a custom domain would "
                     f"undo that. Remove the internal flag first if it really "
                     f"should be reachable from the internet.")
        owner = await c.fetchval(
            "SELECT id FROM apps WHERE $1 = ANY(custom_domains)", d)
        if owner:
            raise HTTPException(
                409, f"{d} is already registered to '{owner}'"
                     if owner != app_id else f"{app_id} already answers on {d}")
        domains = sorted({*(row["custom_domains"] or []), d})
        out = await _apply(c, row, domains)
    return {**out, "added": d, "dns": DNS_NOTE}


@router.delete("/{app_id}/domains/{domain}", operation_id="apps_domain_remove",
               summary="Stop an app answering on a custom domain")
async def remove_domain(app_id: str, domain: str):
    """Remove a custom domain. The app keeps its generated hostname and stays
    reachable there, so this never takes an app offline.

    Routing is updated immediately: the hostname is dropped from the static
    host's router and from the app's own, after which it reaches Traefik's
    catchall and 503s. Delete the DNS record too — nothing here can, and a record
    left pointing at this box hands your name to whatever answers the catchall.
    """
    d = normalise(domain)
    async with pool().acquire() as c:
        row = await _row(c, app_id)
        current = list(row["custom_domains"] or [])
        if d not in current:
            raise HTTPException(404, f"{app_id} does not answer on {d}")
        out = await _apply(c, row, [x for x in current if x != d])
    return {**out, "removed": d}
