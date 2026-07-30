"""GitHub Actions secrets — CRUD the GitHub MCP can't do.

Fills the one gap a model hits when wiring CI: setting a repository secret needs
a libsodium-encrypted PUT, which no GitHub connector tool exposes. This does it
with the platform's GitHub token. Listing returns names only — GitHub never
hands back a secret's value.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_key
from .. import github_api

router = APIRouter(prefix="/github", tags=["github"],
                   dependencies=[Depends(require_key)])


class SecretValue(BaseModel):
    value: str = Field(
        description="The secret's plaintext value. It is encrypted with the "
                    "repo's public key before it leaves this platform and is "
                    "never stored or returned — but it does pass through this "
                    "tool call, so treat it as sensitive.")


def _translate(e: "github_api.GitHubError"):
    msg = str(e)
    if "-> 404" in msg:
        raise HTTPException(404, "no such repo, or the token cannot see it")
    if "-> 403" in msg or "-> 401" in msg:
        raise HTTPException(
            403, "the platform's GitHub token lacks access — it needs `repo` "
                 "scope (classic) or a fine-grained token with Secrets: write")
    if "-> 422" in msg:  # usually an invalid secret name
        raise HTTPException(422, f"GitHub rejected the request: {msg}")
    raise HTTPException(502, msg)


@router.get("/{owner}/{repo}/secrets", operation_id="github_secrets_list",
            summary="List a repository's GitHub Actions secret names")
async def secrets_list(owner: str, repo: str):
    """The names (and last-updated times) of a repo's Actions secrets. Values are
    never returned — GitHub does not expose them. Use this to check whether a
    secret already exists before setting it."""
    try:
        return {"repo": f"{owner}/{repo}",
                "secrets": await github_api.list_secrets(owner, repo)}
    except github_api.GitHubError as e:
        _translate(e)


@router.put("/{owner}/{repo}/secrets/{name}", operation_id="github_secret_set",
            summary="Create or update a repository's GitHub Actions secret")
async def secret_set(owner: str, repo: str, name: str, body: SecretValue):
    """Create or overwrite an Actions secret. The value is encrypted with the
    repo's public key before upload. Same call for create and update — an
    existing secret of that name is replaced. The value passes through this call,
    so avoid echoing it back to the user afterwards.

    GitHub secret names are case-insensitive and stored in UPPERCASE, so the name
    is uppercased to match (github_secret_set('foo') -> FOO), keeping set, list
    and delete consistent. Names must be alphanumeric/underscore and not start
    with a digit or 'GITHUB_'.
    """
    name = name.upper()
    try:
        await github_api.set_secret(owner, repo, name, body.value)
    except github_api.GitHubError as e:
        _translate(e)
    return {"repo": f"{owner}/{repo}", "name": name, "set": True}


@router.delete("/{owner}/{repo}/secrets/{name}", operation_id="github_secret_delete",
               summary="Delete a repository's GitHub Actions secret")
async def secret_delete(owner: str, repo: str, name: str):
    """Delete an Actions secret (name uppercased to match GitHub's storage).
    Irreversible, but a secret is just re-set with github_secret_set if needed."""
    name = name.upper()
    try:
        await github_api.delete_secret(owner, repo, name)
    except github_api.GitHubError as e:
        _translate(e)
    return {"repo": f"{owner}/{repo}", "name": name, "deleted": True}
