"""Shared utils: pooled HTTP client, path/SMB translation, queue/completeness helpers."""
from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
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


_background_tasks: set[asyncio.Task] = set()


def spawn_task(coro) -> asyncio.Task:
    """create_task + a strong reference held until the task finishes. The event
    loop keeps only a WEAK reference to tasks, so a bare create_task(...) whose
    result is discarded can be garbage-collected mid-execution (documented
    asyncio pitfall). The fire-and-forget callers here are the react-immediately
    search paths — a vanished task looks like the fast path silently not firing."""
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t


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


def _try_smb(fs_path: str, mapping: PathMap) -> Optional[str]:
    """fs_to_smb, or None when the path isn't under linux_root."""
    try:
        return fs_to_smb(fs_path, mapping)
    except Exception:
        return None


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


def _sfv_verified_complete(dir_fs: Path) -> Optional[bool]:
    """Cross-checks a release folder's .sfv manifest against what's actually on
    disk — every filename the manifest lists must be present. Returns True
    (verified complete), False (manifest present but something's missing — a
    real incomplete/corrupt grab even if the LAST volume happens to exist,
    which is all _release_complete alone checks for), or None (no readable
    .sfv here; caller should fall back to _release_complete's weaker
    last-file-presence heuristic).

    _release_complete alone was fooled live (Underworld: Evolution, 2026-08-
    22): AirDC++ reported the grab "completed" with 2 of 87 expected RAR
    volumes actually on disk — but one of the two was the LAST volume
    (.rar), the only thing _release_complete looks for. Reads directly off
    dc-bridge's own read-only bind mount of the real backing storage (the
    same physical files AirDC++/rargate write to), not via AirDC++'s webapi
    — dc-bridge already has this access, so no new "read remote file
    content" capability is needed there."""
    try:
        entries = list(dir_fs.iterdir())
    except OSError:
        return None
    sfv_files = [p for p in entries if p.suffix.lower() == ".sfv"]
    if not sfv_files:
        return None
    present = {p.name.lower() for p in entries if p.is_file()}
    expected: set[str] = set()
    try:
        with open(sfv_files[0], "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(";"):
                    continue
                # "filename.ext CRC32HEX" — filename may contain spaces, the
                # CRC is always the last whitespace-separated token.
                parts = line.rsplit(None, 1)
                if len(parts) != 2:
                    continue
                expected.add(parts[0].strip().lower())
    except OSError:
        return None
    if not expected:
        return None
    return expected.issubset(present)


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


