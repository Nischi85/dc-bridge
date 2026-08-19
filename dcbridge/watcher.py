"""Watches the REAL media storage for out-of-band deletions.

Radarr/Sonarr are pointed at a rargate/rar2fs FUSE view of the library, which
doesn't support unlink() — deleting a file has to happen on the real backing
storage directly, which *arr never sees or gets told about. Left alone, *arr's
DB keeps believing the file is still there (hasFile stays true) forever, so
the bridge never re-tracks or re-searches it either.

This module builds a real-directory -> item_id index from *arr's own path
records (Radarr's movie.path, Sonarr's per-episode episodefile.path — both
already point at the real scene-release folder name, see reconcile_movie_path
and the Sonarr importer), then watches watch_root with inotify. A delete event
under an indexed directory fires a targeted *arr rescan, so hasFile flips to
false and the item falls back into the bridge's normal sync/search flow on its
own — closing the loop without a manual rescan step.
"""
from __future__ import annotations
import logging
import time
from pathlib import Path
from typing import Optional
import httpx
from watchfiles import Change, awatch
log = logging.getLogger("dc_bridge")

from dcbridge.config import Config
from dcbridge.util import arr_to_fs, http_session
from dcbridge.arr import trigger_arr_rescan


async def _fetch_movie_index(cfg: Config, http: httpx.AsyncClient) -> dict[str, str]:
    """real dir (no trailing slash) -> 'radarr:<id>', for every movie *arr
    currently believes has a file."""
    if not cfg.radarr.api_key:
        return {}
    url = cfg.radarr.url.rstrip("/")
    h = {"X-Api-Key": cfg.radarr.api_key}
    r = await http.get(f"{url}/api/v3/movie", headers=h)
    if r.status_code != 200:
        log.warning("fs_watch: GET radarr movies -> %s", r.status_code)
        return {}
    idx: dict[str, str] = {}
    for m in r.json():
        if not m.get("hasFile") or not m.get("path"):
            continue
        real = arr_to_fs(m["path"], cfg.path_translate).rstrip("/")
        idx[real] = f"radarr:{m['id']}"
    return idx


async def _fetch_series_index(cfg: Config, http: httpx.AsyncClient) -> dict[str, str]:
    """real dir (no trailing slash) -> 'sonarr:<id>', one entry per episode
    FILE's containing folder, for every series *arr currently has file(s) for."""
    if not cfg.sonarr.api_key:
        return {}
    url = cfg.sonarr.url.rstrip("/")
    h = {"X-Api-Key": cfg.sonarr.api_key}
    r = await http.get(f"{url}/api/v3/series", headers=h)
    if r.status_code != 200:
        log.warning("fs_watch: GET sonarr series -> %s", r.status_code)
        return {}
    idx: dict[str, str] = {}
    for s in r.json():
        if not (s.get("statistics") or {}).get("episodeFileCount"):
            continue
        sid = s["id"]
        fr = await http.get(f"{url}/api/v3/episodefile", params={"seriesId": sid}, headers=h)
        if fr.status_code != 200:
            log.warning("fs_watch: GET sonarr episodefile seriesId=%s -> %s", sid, fr.status_code)
            continue
        for ef in fr.json():
            path = ef.get("path")
            if not path:
                continue
            real = arr_to_fs(path, cfg.path_translate).rstrip("/")
            idx[str(Path(real).parent)] = f"sonarr:{sid}"
    return idx


async def _build_index(cfg: Config) -> dict[str, str]:
    async with http_session() as http:
        movies = await _fetch_movie_index(cfg, http)
        series = await _fetch_series_index(cfg, http)
    idx = {**movies, **series}
    log.info(
        "fs_watch: reindexed %d real dir(s) (%d movie, %d series-episode)",
        len(idx), len(movies), len(series),
    )
    return idx


def _match(index: dict[str, str], deleted_path: str, watch_root: Path) -> tuple[Optional[Path], Optional[str]]:
    """Walk from the deleted path itself up to watch_root, returning the
    (indexed directory, item_id) of the first indexed entry found — covers
    both a whole release folder being rm -rf'd (its own path is indexed) and
    individual files being deleted one by one (their containing folder is
    indexed). The caller still has to confirm the returned directory is
    actually gone (see fs_watch_loop) — a delete event alone doesn't mean
    that: AirDC++ itself deletes/renames .rNN parts and temp files as a normal
    part of extracting a download IN that same folder, which would otherwise
    misfire a rescan mid-download."""
    cur = Path(deleted_path)
    while True:
        item = index.get(str(cur))
        if item:
            return cur, item
        if cur == watch_root or watch_root not in cur.parents:
            return None, None
        cur = cur.parent


async def fs_watch_loop(app) -> None:
    cfg: Config = app.state.cfg
    wc = cfg.fs_watch
    if not (wc.enabled and wc.watch_root):
        return
    root = Path(wc.watch_root.rstrip("/"))
    if not root.is_dir():
        log.warning(
            "fs_watch: watch_root %s doesn't exist/isn't mounted — watcher not starting "
            "(check the container's bind mount)", root,
        )
        return
    log.info("fs_watch: watching %s for out-of-band deletions", root)
    index = await _build_index(cfg)
    last_reindex = time.time()

    async for changes in awatch(root, recursive=True, debounce=int(wc.debounce_seconds * 1000)):
        if time.time() - last_reindex >= wc.reindex_interval_seconds:
            index = await _build_index(cfg)
            last_reindex = time.time()
        # Candidate (indexed_dir, item_id) pairs from this batch's delete events —
        # a dict so multiple events for the same directory collapse to one check.
        candidates: dict[Path, str] = {}
        for change_type, changed_path in changes:
            if change_type != Change.deleted:
                continue
            indexed_dir, item_id = _match(index, changed_path, root)
            if item_id:
                candidates[indexed_dir] = item_id
        for indexed_dir, item_id in candidates.items():
            # A delete event doesn't mean the release is gone — AirDC++ itself
            # deletes/renames .rNN parts as a normal part of extracting a fresh
            # download into this same folder. Only act once the indexed
            # directory has genuinely disappeared, not on transient churn.
            if indexed_dir.exists():
                log.debug(
                    "fs_watch: %s still on disk after delete event(s) under it "
                    "(%s) — treating as in-progress churn, not a real deletion",
                    indexed_dir, item_id,
                )
                continue
            log.info("fs_watch: %s deleted out-of-band — triggering rescan", item_id)
            await trigger_arr_rescan(cfg, item_id)
