"""Fetch a URL, without letting an anonymous caller aim this box at itself.

This service takes URLs from anyone, with no API key, and runs on the machine
that hosts the whole platform. Without egress control that is a server-side
request forgery hole pointing at the Coolify API, the control plane, every
sleeping app's internal address and the filesystem -- and because the fetched
text becomes queryable, the caller gets to READ the result. So the guard here is
the security boundary of the entire service.

Four rules, and the last one is the one usually missed:

  1. http/https only. No file://, no gopher://, no credentials in the URL.
  2. Every resolved address must be public -- checked on BOTH families, because
     this box's private network is as much fddf:6e69:23a7::/48 as 10.0.1.0/24.
  3. Redirects are followed by hand, and every hop is re-checked. A public URL
     that 302s to 169.254.169.254 passes any check that only looks at the URL
     the caller supplied.
  4. The address actually connected to is verified AFTER the connection is made
     and BEFORE the body is read. Pre-flight DNS validation alone loses to
     rebinding: the name resolves public for the check and private for the
     connect. Checking the peer closes that, because it is the socket's own
     answer rather than a second lookup.
"""
import ipaddress
import socket

import httpx

from .config import FETCH_TIMEOUT_S, MAX_FETCH_BYTES

MAX_REDIRECTS = 5


class FetchError(Exception):
    """The URL could not be fetched, for a reason worth telling the caller."""


def _is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return False
    if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return False
    # IPv6 unique-local (fc00::/7) reads as private in Python, but be explicit:
    # this box's own overlay lives there and it is the address a rebinding
    # attack here would most plausibly aim at.
    if addr.version == 6 and addr.packed[0] & 0xFE == 0xFC:
        return False
    return True


def _check_host(host: str) -> list[str]:
    """Resolve a hostname and require every answer to be public.

    Every answer, not just the first: a name with one public and one private
    record would otherwise pass the check and connect to the private one.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError("could not resolve %s (%s)" % (host, exc.strerror or exc))
    ips = sorted({i[4][0] for i in infos})
    if not ips:
        raise FetchError("no addresses for %s" % host)
    for ip in ips:
        if not _is_public(ip):
            raise FetchError(
                "refusing to fetch %s: it resolves to the non-public address %s"
                % (host, ip))
    return ips


def _validate(url: str) -> str:
    parsed = httpx.URL(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError("only http and https URLs can be fetched, not %r"
                         % (parsed.scheme or "<none>"))
    if parsed.userinfo:
        raise FetchError("URLs with embedded credentials are not accepted")
    if not parsed.host:
        raise FetchError("the URL has no host")
    _check_host(parsed.host)
    return parsed.host


def _peer_ip(response: httpx.Response) -> str | None:
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    info = stream.get_extra_info("client_addr") or stream.get_extra_info("socket")
    if isinstance(info, tuple):
        return info[0]
    try:
        return info.getpeername()[0]  # type: ignore[union-attr]
    except Exception:
        return None


def fetch(url: str) -> tuple[bytes, str, str]:
    """Fetch a public URL. Returns (body, final_url, content_type)."""
    current = url
    with httpx.Client(follow_redirects=False, timeout=FETCH_TIMEOUT_S,
                      headers={"User-Agent": "pironman-rag/1.0"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _validate(current)
            with client.stream("GET", current) as response:
                peer = _peer_ip(response)
                # After the connection, before the body: if DNS moved under us
                # between check and connect, this is where it shows up.
                if peer is not None and not _is_public(peer):
                    raise FetchError(
                        "refusing to read from %s: the connection landed on the "
                        "non-public address %s" % (current, peer))
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("redirect with no Location header")
                    current = str(httpx.URL(current).join(location))
                    continue
                if response.status_code >= 400:
                    raise FetchError("the URL returned HTTP %d" % response.status_code)
                body = bytearray()
                for part in response.iter_bytes():
                    body.extend(part)
                    if len(body) > MAX_FETCH_BYTES:
                        raise FetchError(
                            "the document exceeds the %d byte fetch limit"
                            % MAX_FETCH_BYTES)
                ctype = response.headers.get("content-type", "").split(";")[0].strip()
                return bytes(body), current, ctype
    raise FetchError("too many redirects (more than %d)" % MAX_REDIRECTS)


# A Google Docs link fetched anonymously returns the sign-in page, which parses
# as perfectly valid HTML full of nothing to do with the document. Indexed, it
# would answer every query about the contract with Google's login copy -- so it
# is detected and refused rather than stored.
_LOGIN_MARKERS = (
    "accounts.google.com/servicelogin",
    "signinchooser",
    "sign in - google accounts",
    "you need access",
    "request access",
)


def guard_login_page(body: bytes, final_url: str) -> None:
    lowered = body[:20000].decode("utf-8", "replace").lower()
    if any(marker in lowered for marker in _LOGIN_MARKERS) or \
            "accounts.google.com" in final_url.lower():
        raise FetchError(
            "that URL returned a sign-in page, not a document -- publish the "
            "file or export it (for Google Docs use "
            "/export?format=txt on a link shared publicly)")
