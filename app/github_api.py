"""GitHub REST client for repository Actions secrets.

Uses the platform's GitHub token (config.GITHUB_TOKEN). Per the GitHub API a
secret value is encrypted client-side with the repository's public key
(libsodium sealed box) before upload, and GitHub never returns a value back —
so listing shows names only. The token needs `repo` scope (classic) or a
fine-grained token with Secrets: write.
"""
import base64
import httpx

from .config import GITHUB_TOKEN

_API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=_API,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30.0,
    )


async def _request(method: str, path: str, **kw):
    if not GITHUB_TOKEN:
        raise GitHubError("no GitHub token configured (set GITHUB_TOKEN)")
    async with _client() as c:
        r = await c.request(method, path, **kw)
    if r.status_code >= 400:
        raise GitHubError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else None


async def list_secrets(owner: str, repo: str) -> list[dict]:
    data = await _request(
        "GET", f"/repos/{owner}/{repo}/actions/secrets", params={"per_page": 100})
    return [{"name": s["name"], "updated_at": s.get("updated_at")}
            for s in (data or {}).get("secrets", [])]


async def set_secret(owner: str, repo: str, name: str, value: str) -> None:
    # Lazy import so a missing/broken pynacl can never block app startup.
    from nacl import encoding, public
    key = await _request("GET", f"/repos/{owner}/{repo}/actions/secrets/public-key")
    pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    sealed = base64.b64encode(public.SealedBox(pk).encrypt(value.encode())).decode()
    await _request(
        "PUT", f"/repos/{owner}/{repo}/actions/secrets/{name}",
        json={"encrypted_value": sealed, "key_id": key["key_id"]})


async def delete_secret(owner: str, repo: str, name: str) -> None:
    await _request("DELETE", f"/repos/{owner}/{repo}/actions/secrets/{name}")


async def read_file(owner: str, repo: str, path: str) -> str | None:
    """A repository file's text, or None if it is not there.

    None means "not in the repo"; anything else raises. That distinction is the
    whole point — a 404 is an answer, while a 403 from a token that cannot see
    the repo is not, and treating the second as the first is how "this repo has
    no Dockerfile" gets concluded about a repository nobody could read.

    Deliberately not routed through _request: that helper calls .json() on every
    response, and this one asks for the file RAW, so the body is a Dockerfile,
    not JSON.
    """
    if not GITHUB_TOKEN:
        raise GitHubError("no GitHub token configured (set GITHUB_TOKEN)")
    async with _client() as c:
        r = await c.get(f"/repos/{owner}/{repo}/contents/{path}",
                        headers={"Accept": "application/vnd.github.raw"})
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise GitHubError(
            f"GET /repos/{owner}/{repo}/contents/{path} -> {r.status_code}: "
            f"{r.text[:300]}")
    ctype = r.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        # The raw header was ignored, or this path is a directory.
        data = r.json()
        if isinstance(data, dict) and data.get("content"):
            return base64.b64decode(data["content"]).decode("utf-8", "replace")
        return None
    return r.text


async def has_file(owner: str, repo: str, path: str) -> bool:
    return await read_file(owner, repo, path) is not None


async def list_dir(owner: str, repo: str, path: str = "") -> list[dict] | None:
    """A repository directory's entries as `[{"name": ..., "type": ...}, ...]`,
    or None if there is no such directory.

    Same None-vs-raise contract as read_file, and for the same reason: "the
    directory is not there" and "nobody could look" are different answers, and
    a caller that collapses them decides what a repository contains on the
    strength of a 403.

    `type` is GitHub's own — "file", "dir" or "symlink". Only the top level is
    returned; this is not a tree walk.
    """
    if not GITHUB_TOKEN:
        raise GitHubError("no GitHub token configured (set GITHUB_TOKEN)")
    async with _client() as c:
        r = await c.get(f"/repos/{owner}/{repo}/contents/{path}")
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise GitHubError(
            f"GET /repos/{owner}/{repo}/contents/{path} -> {r.status_code}: "
            f"{r.text[:300]}")
    data = r.json()
    # A file path answers with an object rather than a list. That is not a
    # directory, so it is not a listing.
    if not isinstance(data, list):
        return None
    return [{"name": e.get("name", ""), "type": e.get("type", "")}
            for e in data]
