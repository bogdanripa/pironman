"""Frontend bundle storage.

A frontend is a zip of static build output (Vite/CRA `dist/`), unpacked into a
shared volume that the static host (web/server.py) serves from. There is no
container per frontend and no image build: a deploy is an upload plus an atomic
directory swap, so it lands in about a second and never restarts anything.

Alongside each bundle we write `.pironman.json` — the manifest the static host
reads to decide whether the app has a backend to forward unmatched requests to.
Keeping it in the volume means the static host needs no database credentials and
no call back into paas-api.
"""
import json
import shutil
import zipfile
from pathlib import Path

FRONTEND_ROOT = Path("/srv/frontends")

# Refuse absurd bundles rather than filling the Pi's disk.
MAX_FILES = 5000
MAX_BYTES = 200 * 1024 * 1024

# Accepted entry files, in the order the static host prefers them. Keep in sync
# with INDEX_FILES in web/server.py — a bundle accepted here but not recognised
# there would upload cleanly and then serve nothing.
INDEX_FILES = ("index.html", "index.htm")


class FrontendError(RuntimeError):
    pass


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Reject path traversal, absolute paths and oversized archives before
    writing anything to disk."""
    members = [m for m in zf.infolist() if not m.is_dir()]
    if len(members) > MAX_FILES:
        raise FrontendError(f"too many files in bundle ({len(members)} > {MAX_FILES})")
    total = 0
    for m in members:
        name = m.filename.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise FrontendError(f"unsafe path in bundle: {m.filename}")
        total += m.file_size
        if total > MAX_BYTES:
            raise FrontendError("bundle too large (over 200 MB uncompressed)")
    return members


def _strip_root(members: list[zipfile.ZipInfo]) -> str:
    """If everything sits under one top-level directory (the common `dist/`
    case), return it so the bundle is unpacked without that extra level."""
    tops = {m.filename.replace("\\", "/").split("/", 1)[0] for m in members}
    if len(tops) == 1:
        only = tops.pop()
        if any("/" in m.filename.replace("\\", "/") for m in members):
            return only + "/"
    return ""


def deploy_bundle(app_id: str, data: bytes) -> dict:
    """Unpack a zip into the app's frontend directory, atomically.

    The new bundle is staged in a sibling directory and swapped in, so a request
    mid-upload never sees a half-written site.
    """
    import io

    target = FRONTEND_ROOT / app_id
    staging = FRONTEND_ROOT / f".{app_id}.new"
    old = FRONTEND_ROOT / f".{app_id}.old"

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise FrontendError("not a valid zip file")

    members = _safe_members(zf)
    if not members:
        raise FrontendError("bundle is empty")
    root = _strip_root(members)

    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    written = 0
    paths: list[str] = []   # what was published, so the CDN copies can be purged
    for m in members:
        name = m.filename.replace("\\", "/")
        rel = name[len(root):] if root and name.startswith(root) else name
        if not rel:
            continue
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(m) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
        written += 1
        paths.append(rel)

    if not any((staging / n).is_file() for n in INDEX_FILES):
        shutil.rmtree(staging, ignore_errors=True)
        raise FrontendError(
            "bundle has no index.html at its root — zip the contents of your "
            "build output directory (e.g. `cd dist && zip -r ../site.zip .`)")

    # Preserve the manifest across deploys; it is config, not build output.
    manifest = target / ".pironman.json"
    if manifest.is_file():
        shutil.copy2(manifest, staging / ".pironman.json")

    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        target.rename(old)
    staging.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    return {"files": written, "paths": paths}


def deploy_files(app_id: str, files: dict[str, str]) -> dict:
    """Publish a small site from inline text files ({path: content}).

    The zip path is for CI shipping a real build; this is for a site small enough
    to write out directly — a landing page, a redirect stub, a status page — with
    no build step or repository involved. Same atomic staging and swap as
    deploy_bundle, so a request mid-write never sees a partial site.
    """
    if not files:
        raise FrontendError("no files given")
    if len(files) > MAX_FILES:
        raise FrontendError(f"too many files ({len(files)} > {MAX_FILES})")
    total = sum(len(c.encode()) for c in files.values())
    if total > MAX_BYTES:
        raise FrontendError("files too large (over 200 MB)")

    target = FRONTEND_ROOT / app_id
    staging = FRONTEND_ROOT / f".{app_id}.new"
    old = FRONTEND_ROOT / f".{app_id}.old"

    cleaned: dict[str, str] = {}
    for path, content in files.items():
        rel = path.replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            raise FrontendError(f"unsafe path: {path}")
        cleaned[rel] = content
    if not any(n in cleaned for n in INDEX_FILES):
        raise FrontendError("files must include an index.html at the root")

    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for rel, content in cleaned.items():
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)

    manifest = target / ".pironman.json"
    if manifest.is_file():
        shutil.copy2(manifest, staging / ".pironman.json")

    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        target.rename(old)
    staging.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    return {"files": len(cleaned), "paths": sorted(cleaned)}


def write_manifest(app_id: str, has_backend: bool,
                   redirects: list[dict] | None = None,
                   spa: bool = False) -> None:
    """What the static host needs and cannot see on disk: whether this app has a
    backend to forward unmatched requests to, its ordered redirect rules, and
    whether unmatched paths are client-side routes.

    Creates the directory if absent, so an app with redirects but no bundle is
    still served by the static host.
    """
    d = FRONTEND_ROOT / app_id
    d.mkdir(parents=True, exist_ok=True)
    (d / ".pironman.json").write_text(json.dumps({
        "has_backend": has_backend,
        "redirects": redirects or [],
        "spa": spa,
    }))


def info(app_id: str) -> dict | None:
    """What is actually on disk for this app's frontend: file count, total bytes
    and when it was last published. None if no bundle exists.

    Read from the filesystem rather than the database so it reports the truth —
    a bundle that failed to land, or one left behind, shows up as it really is.
    """
    d = FRONTEND_ROOT / app_id
    if not d.is_dir():
        return None
    files, size, newest = 0, 0, 0.0
    for p in d.rglob("*"):
        if p.is_file() and p.name != ".pironman.json":
            st = p.stat()
            files += 1
            size += st.st_size
            newest = max(newest, st.st_mtime)
    if not files:
        return None
    from datetime import datetime, timezone
    return {
        "files": files,
        "size_kb": round(size / 1024, 1),
        "published_at": datetime.fromtimestamp(newest, timezone.utc).isoformat(),
        "has_index": any((d / n).is_file() for n in INDEX_FILES),
    }


def remove(app_id: str) -> None:
    shutil.rmtree(FRONTEND_ROOT / app_id, ignore_errors=True)
