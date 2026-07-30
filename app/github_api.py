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
