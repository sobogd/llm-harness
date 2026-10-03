"""Update distribution: serve the newest built APK + a manifest (no S3).

The mac builds APKs into <repo>/releases/app-<N>.apk (see scripts/build_apk.py).
Version is a plain sequential integer (1, 2, 3, ...) derived from the filename
— no semantic versioning, no crypto versioning. This module scans the releases
directory, picks the highest N, and returns a small manifest with the version,
filename, size and a local sha256 (integrity only, computed on the mac).

The APK file is served over HTTP by the SSE server (/update/...); the bridge on
the VPS streams it back to the phone. Everything flows over the existing
reverse-SSH tunnel — no external bucket, no extra port.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

# <repo>/releases — derived from this file's location, independent of --cwd.
DEFAULT_RELEASES_DIR = Path(__file__).resolve().parent.parent / "releases"

_NAME_RE = re.compile(r"^app-(\d+)\.apk$")

# (filename, mtime, size) -> sha256 : avoid re-hashing a 50MB+ APK on every
# manifest request within the same process run.
_SHA_CACHE: dict[tuple[str, float, int], str] = {}


def _parse_version(path: Path) -> Optional[int]:
    m = _NAME_RE.match(path.name)
    return int(m.group(1)) if m else None


def _sha256(path: Path) -> str:
    st = path.stat()
    key = (path.name, st.st_mtime, st.st_size)
    hit = _SHA_CACHE.get(key)
    if hit is not None:
        return hit
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    _SHA_CACHE[key] = h.hexdigest()
    return h.hexdigest()


def scan_releases(releases_dir: Path = DEFAULT_RELEASES_DIR) -> Optional[dict]:
    """Return the manifest dict for the highest-numbered APK, or None."""
    d = Path(releases_dir)
    if not d.is_dir():
        return None
    best: Optional[dict] = None
    for p in d.glob("*.apk"):
        n = _parse_version(p)
        if n is None:
            continue
        if best is None or n > best["version"]:
            st = p.stat()
            best = {
                "version": n,
                "filename": p.name,
                "size": st.st_size,
                "sha256": _sha256(p),
            }
    return best


def manifest(releases_dir: Path = DEFAULT_RELEASES_DIR) -> dict:
    """Manifest for /update/manifest. version==0 means 'nothing published'."""
    m = scan_releases(releases_dir)
    if m is None:
        return {"version": 0, "filename": "", "size": 0, "sha256": ""}
    return m


def resolve(releases_dir: Path, filename: str) -> Optional[Path]:
    """Safely map a requested filename to a real release file (no traversal)."""
    name = Path(filename).name  # strip any directory part
    if name != filename or not _NAME_RE.match(name):
        return None
    p = Path(releases_dir) / name
    if not p.is_file():
        return None
    return p