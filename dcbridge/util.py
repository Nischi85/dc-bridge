"""Shared utils: pooled HTTP client, path/SMB translation, queue/completeness helpers."""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from typing import Optional
import httpx
log = logging.getLogger("dc_bridge")

# Module-level state/constants (restored after the package split).
_shared_http: Optional[httpx.AsyncClient] = None
_HUB_PATH_SEP = "/"  # AirDC++ results.path uses forward slashes regardless of platform
_VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov")

from dcbridge.config import (
    PathMap,
    PathTranslate,
)
from dcbridge.helpers import (
    episode_keys_from_name,
    release_matches_title,
    release_matches_year,
)


def _get_http() -> httpx.AsyncClient:
    global _shared_http
    if _shared_http is None or _shared_http.is_closed:
        _shared_http = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
    return _shared_http


@asynccontextmanager
async def http_session():
    """Yield the process-wide pooled httpx client (connections reused across calls).
    Intentionally does NOT close it — it lives for the lifetime of the process."""
    yield _get_http()


def arr_to_fs(arr_path: str, rules: list[PathTranslate]) -> str:
    """Apply the first matching arr_prefix rule to translate an *arr path to a host path."""
    p = arr_path.rstrip("/")
    for r in rules:
        ap = r.arr_prefix.rstrip("/")
        if p == ap or p.startswith(ap + "/"):
            tail = p[len(ap):]
            return r.fs_prefix.rstrip("/") + tail
    return p  # no rule matched; return as-is so the caller can detect/log


def fs_to_smb(fs_path: str, mapping: PathMap) -> str:
    """Convert host path under linux_root into a Windows-style SMB path.
    e.g. /mnt/user/media/verified/tv/X  ->  Z:\\verified\\tv\\X
    """
    fs_path = fs_path.rstrip("/")
    root = mapping.linux_root.rstrip("/")
    if not (fs_path == root or fs_path.startswith(root + "/")):
        raise ValueError(f"{fs_path!r} is not under linux_root {root!r}")
    tail = fs_path[len(root):].lstrip("/")
    win = mapping.windows_root.rstrip("\\")
    return win + ("\\" + tail.replace("/", "\\") if tail else "")






# ── *arr webhook handlers ────────────────────────────────────────────────────


# Minimal Sonarr/Radarr webhook bodies. These fields are stable in v3+/v4+;
# unknown fields are ignored (BaseModel default).


def _try_smb(fs_path: str, mapping: PathMap) -> Optional[str]:
    try:
        return fs_to_smb(fs_path, mapping)
    except Exception:
        return None


# ── Event handlers ───────────────────────────────────────────────────────────


def _parent_dir_and_name(path: str) -> tuple[str, str]:
    """Given a result.path like '/TV/Drama/Show.S03/Show.S03E01.RELEASE/file.rar',
    return (parent_dir, release_folder_name) = ('/TV/Drama/Show.S03/Show.S03E01.RELEASE',
    'Show.S03E01.RELEASE'). The parent_dir doubles as the grouping key so all files
    belonging to one release fall into the same bucket.
    """
    parts = [p for p in path.split(_HUB_PATH_SEP) if p]
    if len(parts) < 2:
        return "", ""
    release_folder = parts[-2]
    parent_dir = _HUB_PATH_SEP + _HUB_PATH_SEP.join(parts[:-1])
    return parent_dir, release_folder


def _to_smb_dir(fs_dir: str, mapping: PathMap) -> str:
    """fs path -> SMB directory with required trailing backslash."""
    smb = fs_to_smb(fs_dir, mapping)
    return smb if smb.endswith("\\") else smb + "\\"




def _release_complete(items: list[dict]) -> bool:
    """A queued release folder is 'complete' if it holds the playable video
    directly, or the first RAR volume (.rar). Scene RAR sets download .rar LAST
    (it sorts after .rNN), so its presence is a reliable 'finished' signal — a
    partial set has .r00/.r01/… but no .rar yet."""
    for it in items:
        t = it.get("type") or {}
        if t.get("id") != "file":
            continue
        name = (it.get("name") or "").lower()
        if name.endswith(".rar") or name.endswith(_VIDEO_EXT):
            return True
    return False


def _series_keys_in_queue(bundles: list[dict], title: str) -> set[str]:
    """Episode keys (SxxExx) that already have a bundle in the AirDC++ queue for
    this series — whether the bridge or the user queued them — so we neither
    search the hub for them nor re-grab them."""
    keys: set[str] = set()
    for b in bundles:
        name = b.get("name") or ""
        if release_matches_title(name, title, anchored=True):
            keys.update(episode_keys_from_name(name))
    return keys


def _movie_in_queue(bundles: list[dict], title: str, year) -> bool:
    for b in bundles:
        name = b.get("name") or ""
        if release_matches_title(name, title, anchored=True) and release_matches_year(name, year):
            return True
    return False


def _is_directory_result(r: dict) -> bool:
    t = r.get("type")
    return isinstance(t, dict) and t.get("id") == "directory"


