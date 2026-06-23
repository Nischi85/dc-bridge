"""Sonarr/Radarr/Jellyseerr interaction: sync, auto-approve, rescan, reconcile, availability, children routing."""
from __future__ import annotations
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
import httpx
log = logging.getLogger("dc_bridge")
from dcbridge.config import (
    ChildrenRoutingCfg,
    Config,
)
from dcbridge.helpers import (
    _EPOCH_ISO,
    _fetch_quality_profiles,
    _iso_to_epoch,
    _truncate,
    _utc_iso,
)
from dcbridge.util import (
    arr_to_fs,
    http_session,
)
from dcbridge.state import (
    State,
)


def _is_children_genre(genres: Optional[list], want: list[str]) -> bool:
    g = {x.lower() for x in (genres or [])}
    return any(w.lower() in g for w in want)


def _movie_release_date(m: dict) -> Optional[str]:
    """Web/disc availability date (ISO UTC) — used for both the pre-release gate and
    content-age back-off. Prefer digitalRelease (when WEB scene releases appear),
    then physicalRelease, then inCinemas, then <year>-01-01; None when unknown.
    Deliberately NOT the earliest date: a film that left cinemas months ago but just
    hit digital should count as freshly available, not old."""
    for k in ("digitalRelease", "physicalRelease", "inCinemas"):
        if m.get(k):
            return m[k]
    y = m.get("year")
    return f"{int(y)}-01-01T00:00:00Z" if y else None


def _named_profile_priority(profile_name: str, by_name: dict, app: str) -> Optional[list]:
    """Resolve quality.profile_name to its priority list. None means 'not using a
    named profile' — fall back to each item's own assigned profile. Warns (once per
    sync) if a name is set but absent in this app."""
    if not profile_name:
        return None
    prio = by_name.get(profile_name)
    if prio is None:
        log.warning(
            "%s sync: quality.profile_name %r not found (have %s); "
            "falling back to each item's assigned profile",
            app, profile_name, sorted(n for n in by_name if n),
        )
        return None
    return prio


async def route_children_movie(
    url: str, h: dict, http: httpx.AsyncClient, cr: ChildrenRoutingCfg, movie: dict
) -> Optional[str]:
    """If `movie` is a children's title not already under the children's movies
    root, relocate it in Radarr (rootFolderPath + path, moveFiles=false) and return
    the new rootFolderPath. Returns None when disabled / not children's / already
    routed / already has a file (don't orphan an existing download)."""
    if not (cr.movies_root and cr.genres):
        return None
    if not _is_children_genre(movie.get("genres"), cr.genres):
        return None
    new_root = cr.movies_root.rstrip("/")
    if (movie.get("rootFolderPath") or "").rstrip("/") == new_root:
        return None
    if movie.get("hasFile"):
        log.debug("children-route: movie %r already has a file; leaving it put", movie.get("title"))
        return None
    folder = Path(movie.get("path") or "").name
    body = dict(movie)
    body["rootFolderPath"] = new_root
    body["path"] = f"{new_root}/{folder}" if folder else new_root
    try:
        r = await http.put(
            f"{url}/api/v3/movie/{movie['id']}", headers=h,
            params={"moveFiles": "false"}, json=body,
        )
        if r.status_code in (200, 201, 202):
            log.info("children-route: moved movie %r -> %s", movie.get("title"), body["path"])
            return new_root
        log.warning("children-route: PUT movie %s -> %s %s", movie.get("id"), r.status_code, _truncate(r.text))
    except Exception as e:
        log.warning("children-route movie %s failed: %s", movie.get("id"), e)
    return None


async def route_children_series(
    url: str, h: dict, http: httpx.AsyncClient, cr: ChildrenRoutingCfg, series: dict
) -> Optional[str]:
    """As route_children_movie but for a Sonarr series — relocate to the children's
    series root and return the new series path. Skips when the series already has
    any episode file (don't orphan existing downloads)."""
    if not (cr.series_root and cr.genres):
        return None
    if not _is_children_genre(series.get("genres"), cr.genres):
        return None
    new_root = cr.series_root.rstrip("/")
    cur_path = (series.get("path") or "").rstrip("/")
    if cur_path == new_root or cur_path.startswith(new_root + "/"):
        return None
    if (series.get("statistics") or {}).get("episodeFileCount"):
        log.debug("children-route: series %r already has file(s); leaving it put", series.get("title"))
        return None
    folder = Path(cur_path).name if cur_path else ""
    new_path = f"{new_root}/{folder}" if folder else new_root
    body = dict(series)
    body["rootFolderPath"] = new_root
    body["path"] = new_path
    try:
        r = await http.put(
            f"{url}/api/v3/series/{series['id']}", headers=h,
            params={"moveFiles": "false"}, json=body,
        )
        if r.status_code in (200, 201, 202):
            log.info("children-route: moved series %r -> %s", series.get("title"), new_path)
            return new_path
        log.warning("children-route: PUT series %s -> %s %s", series.get("id"), r.status_code, _truncate(r.text))
    except Exception as e:
        log.warning("children-route series %s failed: %s", series.get("id"), e)
    return None


async def _sync_sonarr(cfg: Config, state: State, http: httpx.AsyncClient) -> dict:
    """Pull all monitored series from sonarr, register as tracked items, and
    mark each already-downloaded episode as completed.
    """
    url = cfg.sonarr.url.rstrip("/")
    key = cfg.sonarr.api_key
    if not key:
        return {"error": "sonarr.api_key not configured"}
    h = {"X-Api-Key": key}
    r = await http.get(f"{url}/api/v3/series", headers=h)
    r.raise_for_status()
    series_list = r.json()
    profiles_by_id, profiles_by_name = await _fetch_quality_profiles(url, h, http)
    named_priority = _named_profile_priority(cfg.quality.profile_name, profiles_by_name, "sonarr")
    added = 0
    skipped = 0
    pre_completed = 0
    for s in series_list:
        if not s.get("monitored"):
            skipped += 1
            continue
        sid = s["id"]
        title = s.get("title") or "?"
        # Children's-genre routing: relocate the series to the kids' Sonarr root
        # before we derive the target, so the download nests under it.
        routed = await route_children_series(url, h, http, cfg.children_routing, s)
        if routed:
            s["path"] = routed
        arr_path = s.get("path") or ""
        if not arr_path:
            log.warning("sonarr sync: series %s (%s) has no path; skipping", sid, title)
            skipped += 1
            continue
        target_dir_fs = arr_to_fs(arr_path, cfg.path_translate)
        await state.add_item(
            id_=f"sonarr:{sid}",
            kind="tv",
            title=title,
            target_dir_fs=target_dir_fs,
            monitored_keys=None,
        )
        added += 1
        # Walk every episode: mark already-downloaded ones completed, and from the
        # monitored-but-missing ones derive the air-date gate — air_anchor_utc =
        # newest wanted episode that has ALREADY aired; next_air_utc = soonest
        # wanted episode still to air. Season 0 specials are ignored (never queued).
        rep = await http.get(f"{url}/api/v3/episode", params={"seriesId": sid}, headers=h)
        air_anchor: str | None = None
        next_air: str | None = None
        wanted_keys: list[str] = []  # monitored, no file, already aired (+offset)
        now_e = int(time.time())
        offset = int(cfg.poller.air_offset_hours * 3600)
        if rep.status_code == 200:
            for ep in rep.json():
                season = ep.get("seasonNumber")
                epnum = ep.get("episodeNumber")
                if season is None or epnum is None:
                    continue
                ekey = f"S{int(season):02d}E{int(epnum):02d}"
                if ep.get("hasFile"):
                    await state.mark_completed(
                        f"sonarr:{sid}", ekey, None, "(pre-existing)"
                    )
                    pre_completed += 1
                    continue
                if not ep.get("monitored") or int(season) == 0:
                    continue
                air = ep.get("airDateUtc")
                if not air:
                    # Undated (TBA) -> treat as available now (sentinel anchor).
                    if air_anchor is None:
                        air_anchor = _EPOCH_ISO
                    wanted_keys.append(ekey)
                    continue
                # Searchable only at airdate + configured offset.
                eff_e = _iso_to_epoch(air) + offset
                eff = _utc_iso(eff_e)
                if eff_e > now_e:
                    if next_air is None or eff < next_air:
                        next_air = eff
                else:
                    if air_anchor is None or eff > air_anchor:
                        air_anchor = eff
                    wanted_keys.append(ekey)
        await state.set_tv_air(f"sonarr:{sid}", air_anchor, next_air)
        await state.set_monitored_keys(f"sonarr:{sid}", wanted_keys)
        await state.set_quality_priority(
            f"sonarr:{sid}",
            named_priority if named_priority is not None
            else (profiles_by_id.get(s.get("qualityProfileId")) or []),
        )
    log.info(
        "sonarr sync: %d series tracked, %d skipped, %d episodes pre-marked completed",
        added,
        skipped,
        pre_completed,
    )
    return {"tracked": added, "skipped": skipped, "pre_completed_episodes": pre_completed}


async def _sync_radarr(cfg: Config, state: State, http: httpx.AsyncClient) -> dict:
    """Pull all monitored movies from radarr, register as tracked items, and
    mark already-downloaded movies as completed.
    """
    url = cfg.radarr.url.rstrip("/")
    key = cfg.radarr.api_key
    if not key:
        return {"error": "radarr.api_key not configured"}
    h = {"X-Api-Key": key}
    r = await http.get(f"{url}/api/v3/movie", headers=h)
    r.raise_for_status()
    movies = r.json()
    profiles_by_id, profiles_by_name = await _fetch_quality_profiles(url, h, http)
    named_priority = _named_profile_priority(cfg.quality.profile_name, profiles_by_name, "radarr")
    added = 0
    skipped = 0
    pre_completed = 0
    for m in movies:
        if not m.get("monitored"):
            skipped += 1
            continue
        mid = m["id"]
        title = m.get("title") or "?"
        # Children's-genre routing: relocate the movie to the kids' Radarr root
        # before we derive the target, so the download lands under it.
        routed = await route_children_movie(url, h, http, cfg.children_routing, m)
        if routed:
            m["rootFolderPath"] = routed
        # Use the root folder, not the "Title (Year)" wrapper — see handler note.
        arr_root = m.get("rootFolderPath") or ""
        if not arr_root:
            fp = m.get("folderPath") or m.get("path") or ""
            if fp:
                arr_root = str(Path(fp).parent)
        if not arr_root:
            log.warning("radarr sync: movie %s (%s) has no root/folder path; skipping", mid, title)
            skipped += 1
            continue
        target_dir_fs = arr_to_fs(arr_root, cfg.path_translate)
        await state.add_item(
            id_=f"radarr:{mid}",
            kind="movie",
            title=title,
            target_dir_fs=target_dir_fs,
            monitored_keys=["movie"],
            year=m.get("year"),
        )
        await state.set_quality_priority(
            f"radarr:{mid}",
            named_priority if named_priority is not None
            else (profiles_by_id.get(m.get("qualityProfileId")) or []),
        )
        await state.set_release_date(f"radarr:{mid}", _movie_release_date(m))
        added += 1
        if m.get("hasFile"):
            await state.mark_completed(
                f"radarr:{mid}", "movie", None, "(pre-existing)"
            )
            pre_completed += 1
    log.info(
        "radarr sync: %d movies tracked, %d skipped, %d pre-marked completed",
        added,
        skipped,
        pre_completed,
    )
    return {"tracked": added, "skipped": skipped, "pre_completed_movies": pre_completed}


async def _count_requested_episodes(
    base: str, h: dict, http: httpx.AsyncClient, tmdb_id, seasons: list,
) -> Optional[int]:
    """Total episodes across the requested season numbers (season 0 specials
    excluded), via Jellyseerr's TV details. None if it can't be determined."""
    want = {s.get("seasonNumber") for s in (seasons or []) if s.get("seasonNumber")}
    if not tmdb_id or not want:
        return None
    try:
        r = await http.get(f"{base}/api/v1/tv/{tmdb_id}", headers=h)
        if r.status_code != 200:
            return None
        total = 0
        for s in (r.json().get("seasons") or []):
            if s.get("seasonNumber") in want:
                total += int(s.get("episodeCount") or 0)
        return total
    except Exception:
        return None


async def auto_approve_requests(cfg: Config, http: httpx.AsyncClient) -> dict:
    """Approve PENDING Jellyseerr requests so they flow into *arr and download:
    movies always; a TV request only when its requested seasons total
    <= tv_max_episodes. Runs before the syncs so newly-approved items are picked
    up the same cycle. No-op unless auto_approve.enabled."""
    aa = cfg.auto_approve
    js = cfg.jellyseerr
    if not (aa.enabled and js.url and js.api_key):
        return {"skipped": "disabled"}
    base = js.url.rstrip("/")
    h = {"X-Api-Key": js.api_key}
    approved = 0
    left_pending = 0
    skip = 0
    while True:
        try:
            r = await http.get(
                f"{base}/api/v1/request",
                params={"filter": "pending", "take": 100, "skip": skip},
                headers=h,
            )
        except Exception as e:
            log.warning("auto-approve: list pending failed: %s", e)
            break
        if r.status_code != 200:
            log.warning("auto-approve: list pending -> %s", r.status_code)
            break
        body = r.json()
        results = body.get("results", []) if isinstance(body, dict) else []
        if not results:
            break
        for req in results:
            req_id = req.get("id")
            media = req.get("media") or {}
            mtype = media.get("mediaType") or req.get("type")
            if mtype == "movie":
                ok, reason = True, "movie"
                eps = None
            elif mtype == "tv":
                eps = await _count_requested_episodes(
                    base, h, http, media.get("tmdbId"), req.get("seasons")
                )
                ok = eps is not None and eps <= aa.tv_max_episodes
                reason = f"tv {eps} ep"
            else:
                ok, reason, eps = False, mtype, None
            if not ok:
                left_pending += 1
                log.info("auto-approve: leaving request %s pending (%s > %s ep)",
                         req_id, eps, aa.tv_max_episodes)
                continue
            try:
                ar = await http.post(f"{base}/api/v1/request/{req_id}/approve", headers=h)
                if ar.status_code in (200, 201):
                    approved += 1
                    log.info("auto-approve: approved request %s (%s)", req_id, reason)
                else:
                    log.warning("auto-approve: approve %s -> %s %s",
                                req_id, ar.status_code, _truncate(ar.text))
            except Exception as e:
                log.warning("auto-approve: approve %s failed: %s", req_id, e)
        page_info = body.get("pageInfo") or {}
        total = int(page_info.get("results") or 0)
        skip += 100
        if skip >= total:
            break
    if approved or left_pending:
        log.info("auto-approve: approved=%d left_pending=%d", approved, left_pending)
    return {"approved": approved, "left_pending": left_pending}


async def _sync_jellyseerr(cfg: Config, state: State, http: httpx.AsyncClient) -> dict:
    """Stamp each tracked_item with its Jellyseerr request status (when active).

    Resets all request_status to NULL first, then for each filter in
    `active_statuses` fetches the matching requests (paginated) and sets
    request_status on the matched arr items via media.externalServiceId.

    Match rule:
      Jellyseerr request -> ('movie' or 'tv') + media.externalServiceId
                         -> bridge tracked_item id 'radarr:<id>' / 'sonarr:<id>'
    """
    js = cfg.jellyseerr
    if not js.url or not js.api_key:
        return {"skipped": "jellyseerr.url/api_key not configured"}
    h = {"X-Api-Key": js.api_key}
    base = js.url.rstrip("/")

    await state.clear_all_request_statuses()
    per_status: dict[str, int] = {}
    matched_total = 0
    unmatched_total = 0
    for status in js.active_statuses:
        seen_in_status = 0
        skip = 0
        page_size = 100
        while True:
            r = await http.get(
                f"{base}/api/v1/request",
                params={"filter": status, "take": page_size, "skip": skip},
                headers=h,
            )
            if r.status_code != 200:
                log.warning(
                    "jellyseerr: filter=%s skip=%d -> %s %s",
                    status,
                    skip,
                    r.status_code,
                    _truncate(r.text),
                )
                break
            body = r.json()
            results = body.get("results", []) if isinstance(body, dict) else []
            if not results:
                break
            for req in results:
                seen_in_status += 1
                media = req.get("media") or {}
                ext_id = media.get("externalServiceId")
                mtype = media.get("mediaType") or req.get("type")
                if ext_id is None or mtype not in ("movie", "tv"):
                    unmatched_total += 1
                    continue
                # Jellyseerr media.status: 1 unknown · 2 pending · 3 processing ·
                # 4 partially available · 5 available. Skip fully-available items
                # for both movies AND TV so the active worklist matches what
                # Jellyseerr's UI calls "not (fully) available". For ongoing TV,
                # when sonarr discovers a new episode it updates Jellyseerr and
                # the series flips back to status=4 (partially available); the
                # auto-sync 15min later puts it back into the worklist.
                media_status = media.get("status")
                if media_status == 5:
                    unmatched_total += 1
                    continue
                prefix = "radarr" if mtype == "movie" else "sonarr"
                item_id = f"{prefix}:{ext_id}"
                ok = await state.set_request_status(item_id, status)
                if ok:
                    matched_total += 1
                    # Remember the Jellyseerr media id so the bridge can flip the
                    # request to "available" once its download lands on disk.
                    await state.set_jellyseerr_media_id(item_id, media.get("id"))
                    # Stamp the request's createdAt so age-based back-off has
                    # a reference timestamp. Jellyseerr returns ISO Z strings.
                    created = req.get("createdAt")
                    if created:
                        try:
                            ts = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp())
                            await state.set_request_created_at(item_id, ts)
                        except Exception:
                            pass
                else:
                    unmatched_total += 1
            page_info = body.get("pageInfo") or {}
            total = int(page_info.get("results") or 0)
            skip += page_size
            if skip >= total:
                break
        per_status[status] = seen_in_status

    log.info(
        "jellyseerr sync: per-status=%s matched=%d unmatched=%d",
        per_status,
        matched_total,
        unmatched_total,
    )
    return {
        "per_status_count": per_status,
        "matched_to_tracked": matched_total,
        "unmatched": unmatched_total,
    }


async def arr_has_imported(
    cfg: Config, item: dict, completed_keys: Optional[set[str]] = None
) -> Optional[bool]:
    """Has *arr actually imported the bridge's grab? Used to keep the Jellyseerr
    force-available fallback honest — we only force-mark when this is definitively
    False. Returns True (imported — nothing to do), False (not imported — candidate
    for force-mark), or None (unknown/transient error — never act on uncertainty).

    Movie: Radarr `hasFile`. TV: Sonarr has a file for every episode the bridge
    grabbed (`completed_keys`, the SxxExx markers) — judged against only those
    episodes, never the whole series."""
    prefix, _, sid = item["id"].partition(":")
    if not sid.isdigit():
        return None
    try:
        if prefix == "radarr":
            if not cfg.radarr.api_key:
                return None
            base = cfg.radarr.url.rstrip("/")
            async with http_session() as http:
                r = await http.get(
                    f"{base}/api/v3/movie/{sid}",
                    headers={"X-Api-Key": cfg.radarr.api_key},
                )
            if r.status_code != 200:
                return None
            return bool(r.json().get("hasFile"))
        if prefix == "sonarr":
            if not cfg.sonarr.api_key or not completed_keys:
                return None
            base = cfg.sonarr.url.rstrip("/")
            async with http_session() as http:
                r = await http.get(
                    f"{base}/api/v3/episode",
                    params={"seriesId": sid, "includeEpisodeFile": "true"},
                    headers={"X-Api-Key": cfg.sonarr.api_key},
                )
            if r.status_code != 200:
                return None
            have = {
                f"S{int(e.get('seasonNumber', 0)):02d}E{int(e.get('episodeNumber', 0)):02d}"
                for e in r.json() if e.get("hasFile")
            }
            return completed_keys.issubset(have)  # imported iff every grabbed ep has a file
    except Exception as e:
        log.warning("arr_has_imported %s failed: %s", item["id"], e)
    return None


async def mark_jellyseerr_available(cfg: Config, item_id: str, media_id: Optional[int]) -> bool:
    """Flip a Jellyseerr media to 'available' (status 5) — the bridge drops scene
    folders Radarr never imports, so Jellyseerr would otherwise sit on 'processing'
    forever. Idempotent. Returns True on success."""
    if not (cfg.jellyseerr.url and cfg.jellyseerr.api_key and media_id):
        return False
    base = cfg.jellyseerr.url.rstrip("/")
    try:
        async with http_session() as http:
            r = await http.post(
                f"{base}/api/v1/media/{media_id}/available",
                headers={"X-Api-Key": cfg.jellyseerr.api_key},
                json={"is4k": False},
            )
        if r.status_code == 200:
            log.info("poll %s: Jellyseerr media %s marked available", item_id, media_id)
            return True
        log.warning("jellyseerr available media %s -> %s", media_id, r.status_code)
    except Exception as e:
        log.warning("jellyseerr available media %s failed: %s", media_id, e)
    return False


async def trigger_arr_rescan(cfg: Config, item_id: str) -> None:
    """Targeted rescan of ONE series/movie folder (RescanSeries/RescanMovie by id
    — NOT a whole-library scan) so *arr imports the freshly-downloaded files and
    marks them present. That flips Jellyseerr to available AND makes the bridge's
    air-gate stop searching the item (it keys off Sonarr/Radarr hasFile)."""
    prefix, _, sid = item_id.partition(":")
    if prefix == "sonarr":
        base, key, cmd, idk = cfg.sonarr.url, cfg.sonarr.api_key, "RescanSeries", "seriesId"
    elif prefix == "radarr":
        base, key, cmd, idk = cfg.radarr.url, cfg.radarr.api_key, "RescanMovie", "movieId"
    else:
        return
    if not (key and sid.isdigit()):
        return
    try:
        async with http_session() as http:
            r = await http.post(
                f"{base.rstrip('/')}/api/v3/command",
                headers={"X-Api-Key": key, "Content-Type": "application/json"},
                json={"name": cmd, idk: int(sid)},
            )
        if r.status_code in (200, 201):
            log.info("poll %s: triggered targeted %s", item_id, cmd)
        else:
            log.warning("%s for %s -> %s", cmd, item_id, r.status_code)
    except Exception as e:
        log.warning("trigger_arr_rescan %s failed: %s", item_id, e)


async def reconcile_movie_path(cfg: Config, item_id: str, release_name: str) -> None:
    """Point the Radarr movie at the scene download folder so RescanMovie imports
    the file *in place*. Jellyseerr creates the movie with Radarr's own folder name
    (e.g. 'The Odyssey (2026)'), but dc-bridge downloads into the scene-named folder
    ('The.Odyssey.2026.1080p.WEB...'). Without this, the scene folder stays UNMAPPED
    in Radarr's Library Import and the movie stays 'missing'. The source is a
    read-only mount (e.g. a FUSE layer like rargate), so we adopt the folder in
    place (renameMovies/move stays off) — never move/copy.
    Idempotent: skips the PUT once the path already matches. Movies only; TV nests
    its episodes inside the series folder, which RescanSeries already picks up."""
    prefix, _, sid = item_id.partition(":")
    if prefix != "radarr" or not (cfg.radarr.api_key and sid.isdigit()):
        return
    if not release_name or release_name == "(pre-existing)":
        return
    base = cfg.radarr.url.rstrip("/")
    h = {"X-Api-Key": cfg.radarr.api_key, "Content-Type": "application/json"}
    try:
        async with http_session() as http:
            r = await http.get(f"{base}/api/v3/movie/{sid}", headers=h)
            if r.status_code != 200:
                log.warning("reconcile path %s: GET movie -> %s", item_id, r.status_code)
                return
            movie = r.json()
            root = (movie.get("rootFolderPath") or "").rstrip("/")
            if not root:
                cur = movie.get("path") or ""
                root = str(Path(cur).parent) if cur else ""
            if not root:
                log.warning("reconcile path %s: no root folder; skipping", item_id)
                return
            new_path = f"{root}/{release_name}"
            if (movie.get("path") or "").rstrip("/") == new_path:
                return  # already mapped to the scene folder
            movie["path"] = new_path
            pr = await http.put(
                f"{base}/api/v3/movie/{sid}",
                headers=h,
                params={"moveFiles": "false"},
                json=movie,
            )
            if pr.status_code in (200, 201, 202):
                log.info("poll %s: repointed Radarr path -> %s", item_id, new_path)
            else:
                log.warning(
                    "reconcile path %s: PUT movie -> %s %s",
                    item_id, pr.status_code, pr.text[:200],
                )
    except Exception as e:
        log.warning("reconcile_movie_path %s failed: %s", item_id, e)


