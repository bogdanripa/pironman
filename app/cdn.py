"""Cloudflare cache invalidation for frontend deploys.

A published bundle can be served stale from the edge until its TTL expires. With
content-hashed filenames that never matters — new build, new URLs, and the
`no-cache` entry file points at them immediately — but plenty of sites ship
`app.js` and `style.css` unhashed, and those would keep serving the old bytes.

So a deploy purges exactly the URLs it just wrote, rather than the whole zone: a
zone-wide purge would evict every other app's assets too and send the traffic of
the entire box back to origin, to fix one app.

No-op unless CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID are set, so the platform
works without Cloudflare credentials — the cost is that unhashed assets stay
cached for their TTL.
"""
import httpx

from .config import (CLOUDFLARE_API_TOKEN, CLOUDFLARE_ZONE_ID, DOMAIN_SUFFIX)

# Cloudflare accepts at most 30 URLs per purge call.
_BATCH = 30

# Whether this zone's plan allows purge-by-hostname (Enterprise). None until the
# first attempt tells us; cached so we try the good mechanism once, not forever.
_host_purge_supported: bool | None = None


def configured() -> bool:
    return bool(CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID)


def urls_for(app_id: str, paths: list[str]) -> list[str]:
    """Absolute URLs for the files just published, plus the bare host.

    "/" is included because that is what a browser actually requests, and it is
    served from index.html — purging only "/index.html" would leave the cached
    copy of "/" untouched.
    """
    base = f"https://{app_id}{DOMAIN_SUFFIX}"
    out = {f"{base}/"}
    for p in paths:
        rel = p.lstrip("/")
        out.add(f"{base}/{rel}")
        if rel in ("index.html", "index.htm"):
            continue
    return sorted(out)


async def purge(app_id: str, paths: list[str]) -> dict:
    """Drop this app's cached responses from Cloudflare's edge.

    Purging by **hostname** is what we actually want — it clears everything for
    this app in one call, including files a deploy removed and any URL we did not
    write ourselves. Cloudflare gates that (like prefixes and tags) behind
    Enterprise, so we try it and fall back to purging the exact URLs published,
    which every plan supports. The fallback costs one extra request and is
    skipped from then on, since the plan will not change between deploys.

    Never purges the whole zone: that would evict every other app's assets and
    send the entire box's traffic back to origin to fix one app.
    """
    global _host_purge_supported
    if not configured():
        return {"purged": False, "reason": "no Cloudflare credentials configured"}

    endpoint = (f"https://api.cloudflare.com/client/v4/zones/"
                f"{CLOUDFLARE_ZONE_ID}/purge_cache")
    headers = {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"}
    host = f"{app_id}{DOMAIN_SUFFIX}"

    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            if _host_purge_supported is not False:
                r = await c.post(endpoint, headers=headers,
                                 json={"hosts": [host]})
                if r.status_code == 200 and r.json().get("success"):
                    _host_purge_supported = True
                    return {"purged": "host", "host": host}
                _host_purge_supported = False  # not on this plan; stop trying

            urls = urls_for(app_id, paths)
            purged, errors = 0, []
            for i in range(0, len(urls), _BATCH):
                batch = urls[i:i + _BATCH]
                r = await c.post(endpoint, headers=headers, json={"files": batch})
                if r.status_code == 200 and r.json().get("success"):
                    purged += len(batch)
                else:
                    errors.append(f"{r.status_code}: {r.text[:200]}")
    except httpx.HTTPError as e:
        return {"purged": 0, "errors": [str(e)]}

    # A failed purge must not fail the deploy: the files are published and
    # correct, they may just be served stale until their TTL expires.
    return {"purged": purged, "urls": len(urls),
            **({"errors": errors} if errors else {})}
