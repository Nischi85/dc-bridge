"""Download/completion tracking + the per-item poll loop, schedule report, and webhook event handlers."""
from __future__ import annotations
import asyncio
import logging
import random
import re
import time
from pathlib import Path
from typing import Any, Optional
from fastapi import FastAPI
log = logging.getLogger("dc_bridge")

SCHEDULE_FILE = "/config/schedule.txt"  # live snapshot, rewritten each sweep

from dcbridge.config import (
    Config,
)
from dcbridge.helpers import (
    _SEASON_OR_EP_RE,
    _YEAR_RE,
    _fmt_dur,
    _utc_iso,
    compute_cadence,
    episode_keys_from_name,
    is_adult_release,
    is_foreign_language,
    is_sd_release,
    passes_quality,
    release_matches_title,
    release_matches_year,
    release_starts_with_title,
    sanitize_for_dc_search,
    score_result,
)
from dcbridge.util import (
    _HUB_PATH_SEP,
    _is_directory_result,
    _movie_in_queue,
    _parent_dir_and_name,
    _release_complete,
    _series_keys_in_queue,
    _to_smb_dir,
    arr_to_fs,
    http_session,
)
from dcbridge.state import (
    State,
)
from dcbridge.airdcpp import (
    AirDCPP,
)
from dcbridge.arr import (
    _sync_jellyseerr,
    _sync_radarr,
    _sync_sonarr,
    arr_has_imported,
    auto_approve_requests,
    mark_jellyseerr_available,
    reconcile_movie_path,
    trigger_arr_rescan,
)


async def react_to_request(app: FastAPI, item_id: str) -> None:
    """Fired (in the background) when Radarr/Sonarr adds an item — i.e. a request
    was just approved. Stamps Jellyseerr status and searches this one item right
    away, so an approval downloads in seconds instead of waiting up to a full
    poller sweep + sync (~15-30 min)."""
    cfg: Config = app.state.cfg
    state: State = app.state.state
    ad: AirDCPP = app.state.airdcpp
    try:
        if cfg.jellyseerr.url and cfg.jellyseerr.api_key:
            async with http_session() as http:
                await _sync_jellyseerr(cfg, state, http)
        # Re-read so the item carries its freshly-stamped request_status; poll_item
        # reads status off this dict, so a concurrent sync can't race it to NULL.
        item = next((i for i in await state.list_items() if i["id"] == item_id), None)
        if item:
            await poll_item(cfg, state, ad, item)
    except Exception:
        log.exception("react_to_request %s failed", item_id)


async def handle_sonarr_event(app: FastAPI, ev) -> None:
    cfg: Config = app.state.cfg
    state: State = app.state.state
    et = ev.eventType
    if et == "Test":
        return
    if et in {"SeriesAdd", "SeriesAdded", "Series", "SeriesEdit"} and ev.series:
        s = ev.series
        sid = str(s.get("id"))
        title = s.get("title") or "?"
        arr_path = s.get("path") or ""
        if not arr_path:
            log.warning("sonarr %s: no series.path in payload; cannot determine target dir", sid)
            return
        target_dir_fs = arr_to_fs(arr_path, cfg.path_translate)
        if target_dir_fs == arr_path:
            log.warning("sonarr %s (%s): no path_translate rule matched %r", sid, title, arr_path)
        await state.add_item(
            id_=f"sonarr:{sid}",
            kind="tv",
            title=title,
            target_dir_fs=target_dir_fs,
            monitored_keys=None,  # season list TBD via Sonarr API; for now: all
        )
        log.info("tracked TV series: %s -> %s", title, target_dir_fs)
        # React immediately (search now) instead of waiting for the next sweep.
        asyncio.create_task(react_to_request(app, f"sonarr:{sid}"))
    elif et in {"SeriesDelete", "SeriesDeleted"} and ev.series:
        await state.remove_item(f"sonarr:{ev.series.get('id')}")


async def handle_radarr_event(app: FastAPI, ev) -> None:
    cfg: Config = app.state.cfg
    state: State = app.state.state
    et = ev.eventType
    if et == "Test":
        return
    if et in {"MovieAdded", "MovieAdd", "Movie", "MovieEdit"} and ev.movie:
        m = ev.movie
        mid = str(m.get("id"))
        title = m.get("title") or "?"
        # The user's library convention puts release-name folders directly under
        # the Movies root, NOT inside radarr's "Title (Year)" wrapper. So target
        # is the root folder; the bridge appends <release_name>\ at poll time.
        # Prefer the explicit rootFolderPath if radarr provides it (v4+); else
        # derive it as the parent of folderPath.
        arr_root = m.get("rootFolderPath") or ""
        if not arr_root:
            fp = m.get("folderPath") or ""
            if fp:
                arr_root = str(Path(fp).parent)
        if not arr_root:
            log.warning(
                "radarr %s: no rootFolderPath/folderPath in payload; cannot determine target dir",
                mid,
            )
            return
        target_dir_fs = arr_to_fs(arr_root, cfg.path_translate)
        if target_dir_fs == arr_root:
            log.warning("radarr %s (%s): no path_translate rule matched %r", mid, title, arr_root)
        await state.add_item(
            id_=f"radarr:{mid}",
            kind="movie",
            title=title,
            target_dir_fs=target_dir_fs,
            monitored_keys=["movie"],
            year=m.get("year"),
        )
        log.info("tracked movie: %s -> %s", title, target_dir_fs)
        # React immediately (search now) instead of waiting for the next sweep.
        asyncio.create_task(react_to_request(app, f"radarr:{mid}"))
    elif et in {"MovieDelete", "MovieDeleted"} and ev.movie:
        await state.remove_item(f"radarr:{ev.movie.get('id')}")


# ── Poller ───────────────────────────────────────────────────────────────────


async def auto_sync_loop(app: FastAPI) -> None:
    """Periodically re-import items from sonarr/radarr and stamp Jellyseerr
    request statuses, so new requests start getting polled without a manual
    POST /sync. Errors are logged but never crash the loop.
    """
    cfg: Config = app.state.cfg
    state: State = app.state.state
    interval = cfg.auto_sync.interval_seconds
    log.info("auto-sync starting (every %ss)", interval)
    await asyncio.sleep(10)  # let webhook server settle first
    while True:
        try:
            async with http_session() as http:
                # Approve qualifying pending requests first so they reach *arr and
                # are picked up by the syncs below in this same cycle.
                try:
                    await auto_approve_requests(cfg, http)
                except Exception:
                    log.exception("auto-sync: auto-approve failed")
                try:
                    await _sync_sonarr(cfg, state, http)
                except Exception:
                    log.exception("auto-sync: sonarr failed")
                try:
                    await _sync_radarr(cfg, state, http)
                except Exception:
                    log.exception("auto-sync: radarr failed")
                if cfg.jellyseerr.url and cfg.jellyseerr.api_key:
                    try:
                        await _sync_jellyseerr(cfg, state, http)
                    except Exception:
                        log.exception("auto-sync: jellyseerr failed")
            # Refresh the schedule snapshot now that statuses are freshly stamped,
            # so the file is current within seconds of a restart / sync — not only
            # on the next 15-min poller sweep.
            try:
                await write_schedule_report(state, cfg, app.state.airdcpp, int(time.time()))
            except Exception:
                log.exception("auto-sync: schedule report failed")
        except Exception:
            log.exception("auto-sync loop iteration crashed")
        await asyncio.sleep(interval)




async def write_schedule_report(
    state: State, cfg: "Config", ad: AirDCPP, now_ts: int,
    completion: Optional[dict[str, str]] = None,
) -> None:
    """Emit the current search schedule for every active item to the docker log
    and to a live snapshot file (SCHEDULE_FILE). Shows last search, next-due, and
    the reason (gated until air / back-off tier / due now / download state).
    Fetches its own worklist so it can run from the poller sweep AND after each
    sync. An empty worklist (e.g. a sweep that raced the startup sync) is logged
    but does NOT overwrite the file, so a good snapshot is never clobbered.

    `completion` is an optional sweep-scoped {item_id: status} cache so this and
    poll_item don't each fire a separate AirDC++ list_dir for the same movie;
    when None (e.g. from auto-sync), the status is computed live."""
    items = await state.list_items()
    if cfg.jellyseerr.url and cfg.jellyseerr.api_key:
        active = set(cfg.jellyseerr.active_statuses)
        items = [it for it in items if it.get("request_status") in active]
    rows = []
    for it in sorted(items, key=lambda x: (x["kind"], (x["title"] or "").lower())):
        iid, kind = it["id"], it["kind"]
        title = (it["title"] or "")[:34]
        marker = await state.get_completed(iid, "movie") if kind == "movie" else None
        if marker is None:
            comp = None
        elif completion is not None and iid in completion:
            comp = completion[iid]
        else:
            comp = await movie_completion(ad, cfg, it, marker[0], marker[1], now_ts)
        if comp == "complete":
            status, nxt = "done", "downloaded"
        elif comp == "downloading":
            status, nxt = "downloading", f"queued {_fmt_dur(now_ts - marker[1])} ago"
        else:
            # no marker, or a stalled marker we'll prune & re-search
            c = compute_cadence(it, cfg, now_ts)
            status = c["status"]
            tail = "retry: prior grab stalled" if comp == "stalled" else c["detail"]
            if c["due"]:
                nxt = f"now ({tail})"
            elif c["next_due"]:
                nxt = f"in {_fmt_dur(c['next_due'] - now_ts)} ({tail})"
            else:
                nxt = tail
        last = it.get("last_searched_at")
        last_s = f"{_fmt_dur(now_ts - int(last))} ago" if last else "never"
        rows.append(f"  {iid:<12} {kind:<5} {title:<34} {status:<9} last {last_s:<12} next {nxt}")
    report = (
        f"search schedule @ {_utc_iso(now_ts)} — {len(rows)} active item(s)\n"
        + "\n".join(rows)
    )
    log.info("%s", report)
    if not rows:
        return  # don't clobber a good snapshot with a transient/race-empty worklist
    try:
        with open(SCHEDULE_FILE, "w") as f:
            f.write(report + "\n")
    except OSError as e:
        log.warning("could not write schedule file %s: %s", SCHEDULE_FILE, e)


async def poller_loop(app: FastAPI) -> None:
    cfg: Config = app.state.cfg
    state: State = app.state.state
    ad: AirDCPP = app.state.airdcpp
    interval = cfg.poller.interval_seconds
    jitter = cfg.poller.per_item_jitter_seconds
    log.info("poller starting (every %ss, jitter %ss)", interval, jitter)

    # small initial delay so webhooks / *arr can settle on first boot
    await asyncio.sleep(15)

    while True:
        try:
            items = await state.list_items()
            # With the Jellyseerr filter active, only items carrying an active
            # request are relevant. Drop everything else from the worklist so it
            # is neither walked nor allowed to delay the items that matter.
            if cfg.jellyseerr.url and cfg.jellyseerr.api_key:
                active = set(cfg.jellyseerr.active_statuses)
                items = [it for it in items if it.get("request_status") in active]
            log.info("poller sweep: %d item(s) to search", len(items))
            now_ts = int(time.time())
            # One disk probe per movie for the whole sweep, shared by the schedule
            # report and every poll_item, instead of each re-probing the same movie.
            completion = await build_completion_cache(ad, cfg, state, items, now_ts)
            await write_schedule_report(state, cfg, ad, now_ts, completion)
            for it in items:
                searched = False
                try:
                    searched = await poll_item(cfg, state, ad, it, completion)
                except Exception:
                    log.exception("poll_item failed for %s", it.get("id"))
                # Jitter only spreads real AirDC++ searches; skipped or backed-off
                # items cost nothing, so due items are never held back.
                if searched:
                    await asyncio.sleep(random.uniform(0, jitter))
        except Exception:
            log.exception("poller loop iteration crashed")
        await asyncio.sleep(interval)




async def movie_completion(
    ad: AirDCPP, cfg: Config, item: dict, release_name: Optional[str],
    queued_at: int, now_ts: int,
) -> str:
    """Classify a movie's completed marker against what's actually on disk (via
    the AirDC++ filesystem API): 'complete' (file present), 'downloading' (not
    there yet but queued recently — still within the grace window), or 'stalled'
    (gone/partial past grace → the grab failed and should be retried).

    Markers from a Radarr `hasFile=true` sync ('(pre-existing)') and any path we
    can't translate are trusted as complete — they aren't bridge downloads."""
    if not release_name or release_name == "(pre-existing)":
        return "complete"
    try:
        smb = _to_smb_dir(item["target_dir_fs"], cfg.path_map) + release_name + "\\"
    except Exception:
        return "complete"
    items = await ad.list_dir(smb)
    if items is None:
        return "complete"  # AirDC++ error — don't disrupt on a transient failure
    if items and _release_complete(items):
        return "complete"
    grace = int(cfg.poller.download_grace_hours * 3600)
    if now_ts - int(queued_at or 0) < grace:
        return "downloading"
    return "stalled"


async def build_completion_cache(
    ad: AirDCPP, cfg: Config, state: State, items: list[dict], now_ts: int
) -> dict[str, str]:
    """Probe each movie's on-disk completion ONCE per sweep, so write_schedule_report
    and poll_item reuse the result instead of each firing its own AirDC++ list_dir
    for the same movie. Only movies with a completed marker are probed; everything
    else is absent from the map (callers compute live / treat absent as no marker)."""
    cache: dict[str, str] = {}
    for it in items:
        if it["kind"] != "movie":
            continue
        marker = await state.get_completed(it["id"], "movie")
        if marker is None:
            continue
        cache[it["id"]] = await movie_completion(ad, cfg, it, marker[0], marker[1], now_ts)
    return cache


async def remove_completed_bundle(ad: AirDCPP, release_name: str) -> None:
    """Clear a finished download's bundle from the AirDC++ queue (keeps the files
    on disk). Matches by release-folder name; no-op if it's already gone."""
    try:
        for b in (await ad.list_bundles()) or []:
            if b.get("name") == release_name:
                if await ad.remove_bundle(b["id"], remove_finished=False):
                    log.info("removed finished bundle %r from AirDC++ queue", release_name)
                return
    except Exception as e:
        log.warning("remove_completed_bundle %r failed: %s", release_name, e)


async def remove_finished_tv_bundles(
    ad: AirDCPP, state: State, cfg: Config, item: dict, bundles: list[dict]
) -> None:
    """Clear FINISHED episode bundles for this series from the queue (files stay on
    disk). Matches by series title so it tidies both bridge- and user-queued
    episodes. If anything finished, fire ONE targeted *arr rescan for the series
    (debounced: only when something was actually removed, never per-episode)."""
    item_id, title = item["id"], item["title"]
    removed = False
    for b in bundles:
        name = b.get("name") or ""
        st = b.get("status") or {}
        if st.get("completed") and episode_keys_from_name(name) \
                and release_matches_title(name, title, anchored=True):
            if await ad.remove_bundle(b["id"], remove_finished=False):
                # Mark the episode done immediately so we don't re-grab it in the
                # window before the next Sonarr sync sets hasFile=true.
                for ek in episode_keys_from_name(name):
                    await state.mark_completed(item_id, ek, None, name)
                log.info("poll %s: removed finished episode bundle %r from queue", item_id, name)
                removed = True
    if removed:
        await trigger_arr_rescan(cfg, item_id)


async def poll_item(
    cfg: Config, state: State, ad: AirDCPP, item: dict,
    completion: Optional[dict[str, str]] = None,
) -> bool:
    """Run one search round for one tracked item; queue matches not yet completed.

    The scene-release model: each TV episode is its own folder on the hub, holding
    a multi-volume RAR set (.rar/.r00/.r01/...) plus companions (.sfv, .nfo, sample).
    We don't get the folder as one result — we get every file inside it as a
    separate hit. So we:
      1) Group hits by their hub parent directory (the release folder).
      2) Evaluate quality on the RELEASE FOLDER NAME (which carries the WEB / 1080p /
         release group metadata) and on the SUM of file sizes.
      3) Extract the episode key from the release folder name (S03E04 etc.).
      4) Dedup per (item, episode_key) — once an episode is queued, we never
         queue it again, regardless of later/better releases.
      5) Queue every file in the matching release folder with the same
         target_directory so the whole release lands intact next to the media
         library — matching the complete-release layout.
      6) Delete the search instance after.
    """
    title = item["title"]
    kind = item["kind"]
    item_id = item["id"]
    target_dir_fs = item["target_dir_fs"]
    # Quality preference comes from this item's Sonarr/Radarr profile (resolved at
    # sync time); empty falls back to the config quality rules.
    item_priority = item.get("quality_priority") or []

    # Fast-skip movies whose grab is genuinely done. A completed marker only
    # records that we QUEUED a release — verify it actually landed on disk (via
    # AirDC++) before trusting it. A missing/partial grab past the grace window
    # is treated as stalled: prune the marker and re-search (the now-deployed
    # year/season guards stop it re-grabbing the wrong release).
    if kind == "movie":
        marker = await state.get_completed(item_id, "movie")
        if marker:
            rel, qat = marker
            if completion is not None and item_id in completion:
                comp = completion[item_id]
            else:
                comp = await movie_completion(ad, cfg, item, rel, qat, int(time.time()))
            if comp == "complete":
                # Download landed — clear the finished bundle from the queue (files
                # stay on disk) and fire a targeted Radarr rescan so it imports the
                # movie and marks it present (Jellyseerr availability + stops the
                # bridge re-searching it). Availability itself stays *arr/Jellyfin's
                # job; the bridge only nudges the rescan.
                if rel and rel != "(pre-existing)":
                    await remove_completed_bundle(ad, rel)
                    # Repoint Radarr at the scene folder BEFORE the rescan so it
                    # imports the file in place instead of leaving it unmapped.
                    await reconcile_movie_path(cfg, item_id, rel)
                    await trigger_arr_rescan(cfg, item_id)
                    # Verified fallback (opt-in): if Radarr STILL hasn't imported
                    # after the grace window, force the Jellyseerr request to
                    # available so it doesn't sit on "processing" forever.
                    if cfg.jellyseerr.force_available_on_stuck:
                        media_id = item.get("jellyseerr_media_id")
                        grace = cfg.jellyseerr.force_available_grace_hours * 3600
                        if media_id and (int(time.time()) - int(qat or 0)) >= grace \
                                and await arr_has_imported(cfg, item) is False:
                            await mark_jellyseerr_available(cfg, item_id, media_id)
                return False
            if comp == "downloading":
                log.debug("poll %s: movie still downloading, skipping", item_id)
                return False
            log.info("poll %s: prior grab %r stalled/missing — re-searching", item_id, rel)
            await state.clear_completed(item_id, "movie")

    # Verified fallback for stuck TV (opt-in): when the series is done from the
    # air-gate's view but Sonarr never imported the episodes the bridge grabbed,
    # force the Jellyseerr request to available so it doesn't sit on "processing"
    # forever. A still-airing series (cadence != "complete") is never force-marked.
    if kind == "tv" and cfg.jellyseerr.force_available_on_stuck:
        now_ts = int(time.time())
        if compute_cadence(item, cfg, now_ts).get("status") == "complete":
            markers = await state.get_completed_keys(item_id)  # [(key, release_name, queued_at), ...]
            keys = {k for k, _rel, _qat in markers}
            newest = max((int(q or 0) for _k, _r, q in markers), default=0)
            media_id = item.get("jellyseerr_media_id")
            grace = cfg.jellyseerr.force_available_grace_hours * 3600
            if keys and media_id and (now_ts - newest) >= grace \
                    and await arr_has_imported(cfg, item, completed_keys=keys) is False:
                await mark_jellyseerr_available(cfg, item_id, media_id)
                return False

    # Jellyseerr-driven filter: when configured, only poll items currently
    # carrying an active Jellyseerr request. Items without a request stamp
    # (request_status is NULL) are skipped — the bridge becomes a strict
    # Jellyseerr worklist instead of polling everything in *arr.
    if cfg.jellyseerr.url and cfg.jellyseerr.api_key:
        rs = item.get("request_status")
        if rs not in cfg.jellyseerr.active_statuses:
            log.debug(
                "poll %s: request_status=%r not in active list, skipping",
                item_id,
                rs,
            )
            return False

    # TV: snapshot the AirDC++ queue once. Clear finished episode bundles (files
    # stay on disk) + targeted-rescan the series so *arr imports them; and note
    # which episodes already have a bundle so we neither search nor re-grab them
    # (covers both bridge- and user-queued episodes).
    in_queue_keys: set[str] = set()
    needed_keys: set[str] = set()
    if kind == "tv":
        wanted = set(item.get("monitored_keys") or [])
        tv_bundles = await ad.list_bundles() or []
        await remove_finished_tv_bundles(ad, state, cfg, item, tv_bundles)
        in_queue_keys = _series_keys_in_queue(tv_bundles, title)
        # Still needed = wanted, minus what's already in the queue, minus what we
        # already have (completed markers / Sonarr hasFile). This is what we search
        # for and queue; removing a queued (un-finished) episode brings it back.
        done = {k for k, _, _ in await state.get_completed_keys(item_id)}
        needed_keys = wanted - in_queue_keys - done

    now_ts = int(time.time())

    # Schedule decision (TV air-date gate, then age back-off) — shared with the
    # schedule report via compute_cadence so the two never drift.
    decision = compute_cadence(item, cfg, now_ts)
    if not decision["due"]:
        log.debug("poll %s: %s — %s, skipping", item_id, decision["status"], decision["detail"])
        return False
    if decision["status"] == "aired":
        log.info("poll %s: episode aired %s — searching now",
                 item_id, item.get("air_anchor_utc"))

    # Queue-aware: don't spam the DC hubs for anything already in the AirDC++
    # queue (yours or the bridge's). Remove a bundle and it re-enters the search
    # set automatically next sweep (unless *arr now reports it present).
    if kind == "tv":
        if not needed_keys:
            log.info("poll %s: all wanted episodes already queued or downloaded — not searching", item_id)
            return False
    elif kind == "movie" and _movie_in_queue(await ad.list_bundles() or [], title, item.get("year")):
        log.debug("poll %s: movie already in the AirDC++ queue — not searching", item_id)
        return False

    try:
        target_base_smb = _to_smb_dir(target_dir_fs, cfg.path_map)
    except ValueError as e:
        log.error("poll_item: path translation failed for %r: %s", target_dir_fs, e)
        return False

    query = sanitize_for_dc_search(title)
    if query != title:
        log.info("poll %s: sanitized query %r -> %r", item_id, title, query)
    log.info("poll %s [%s] q=%r target=%s", item_id, kind, query, target_base_smb)

    iid = await ad.create_search_instance()
    if iid is None:
        return False
    # Stamp the search time as early as we know we're committing to one — this
    # keeps the back-off honest even if the hub_search itself fails downstream.
    await state.set_last_searched_at(item_id, now_ts)
    try:
        if kind == "tv":
            # Per-episode targeted searches into one instance. A broad "<series>"
            # query hits per-source result caps that hide higher episodes (a
            # source returns only its first ~9 hits, e.g. S01E01-09), so search
            # each still-needed episode specifically to surface its release. Only
            # the not-yet-queued wanted episodes are searched (queue-aware), so it
            # self-limits to a small burst that shrinks as episodes get grabbed.
            for ek in sorted(needed_keys):
                await ad.hub_search(iid, f"{query} {ek}")
            # Results stream in per source; give slower sources time to answer.
            await asyncio.sleep(min(30.0, 10.0 + 2.0 * len(needed_keys)))
        else:
            if not await ad.hub_search(iid, query, extensions=None):
                return True
            await asyncio.sleep(8.0)
        results = await ad.get_results(iid, 0, 500)
        log.info("poll %s: %d hub result(s)", item_id, len(results))

        # Build release-folder candidates. Two result shapes feed the same
        # per-folder bucket (keyed by the release folder's hub path):
        #   - a DIRECTORY result = a whole release folder. Preferred: queue it by
        #     its `id` so AirDC++ pulls the entire folder intact (see queue loop).
        #   - loose FILE results, grouped by their hub parent dir, for hubs that
        #     only return the individual files inside a release.
        groups: dict[str, dict[str, Any]] = {}
        for r in results:
            path = r.get("path") or ""
            if _is_directory_result(r):
                release_name = (r.get("name") or path).rstrip("/").rsplit(_HUB_PATH_SEP, 1)[-1]
                if not release_name:
                    continue
                g = groups.setdefault(
                    path.rstrip("/"),
                    {"release_name": release_name, "files": [], "total_size": 0, "dir_id": None},
                )
                g["dir_id"] = r.get("id")
                g["total_size"] = max(g["total_size"], int(r.get("size") or 0))
            else:
                parent_dir, release_name = _parent_dir_and_name(path)
                if not parent_dir or not release_name:
                    continue
                g = groups.setdefault(
                    parent_dir,
                    {"release_name": release_name, "files": [], "total_size": 0, "dir_id": None},
                )
                g["files"].append(r)
                g["total_size"] += int(r.get("size") or 0)

        # Evaluate each release-folder group as a candidate.
        candidates_by_key: dict[str, list[dict[str, Any]]] = {}
        for parent_dir, g in groups.items():
            release_name: str = g["release_name"]
            total_size: int = g["total_size"]
            if not passes_quality(release_name, total_size, kind, cfg.quality, item_priority):
                continue
            # Adult-content reject: scene porn is tagged XXX and often shares a
            # word with a real title (e.g. "Roccos.World.Feet.Obsession.2.XXX").
            if is_adult_release(release_name, title):
                log.debug("poll %s: skip %r — adult/XXX content", item_id, release_name)
                continue
            # Language guard: reject foreign-language dubs (POLISH, GERMAN,
            # FRENCH, …) so e.g. 'In.The.Cut.2003.POLISH.720p.WEB' isn't grabbed
            # over the English BluRay. See _FOREIGN_LANG_RE for the kept/rejected
            # set (Nordic, East Asian and MULTi tags are not rejected by default).
            if is_foreign_language(release_name):
                log.debug("poll %s: skip %r — foreign-language dub", item_id, release_name)
                continue
            # Movie title guard: the release must START with the movie title
            # (movies have no anchored-phrase guard like TV). Rejects a different
            # film that merely contains the title mid-name — e.g. the right year
            # but wrong movie "WhatsApp.Obsession.The.Murder...2026" for the movie
            # "Obsession", or "Roccos...Obsession.XXX". Scene abbreviation of long
            # subtitles is tolerated (first 2 words), so "Johan.Falk.GSI..." passes.
            if kind == "movie" and not release_starts_with_title(release_name, title):
                log.debug("poll %s: skip %r — not the requested movie (title not at start)",
                          item_id, release_name)
                continue
            # Year guard for movies: a sequel request ("...2", 2026) must not grab
            # the same-title older film. DVD/SD releases legitimately omit the year,
            # so a YEARLESS SD release is allowed; a yearless HD release, or an SD
            # release with a WRONG year, is rejected.
            if kind == "movie" and not release_matches_year(release_name, item.get("year")):
                if not (is_sd_release(release_name) and not _YEAR_RE.search(release_name)):
                    log.debug(
                        "poll %s: skip %r — year mismatch (want %s±1)",
                        item_id, release_name, item.get("year"),
                    )
                    continue
            if kind == "tv":
                # Title guard: the hub search is a loose token match, so a search
                # for "Bad Judge" returns "Judge.Judy..." and "Star City" returns
                # "Star.Trek.Picard...Stardust.City". Reject any release whose name
                # doesn't carry the requested series title as a contiguous phrase.
                if not release_matches_title(release_name, title, anchored=True):
                    log.debug(
                        "poll %s: skip %r — title doesn't match series %r",
                        item_id, release_name, title,
                    )
                    continue
                eks = episode_keys_from_name(release_name)
                if not eks:
                    continue
                for ek in eks:
                    # Skip Season 0 specials (S00Exx) — we only want real seasons.
                    if ek.upper().startswith("S00"):
                        log.debug("poll %s: skip %r — season 0 special", item_id, release_name)
                        continue
                    # Only queue still-needed episodes (wanted, not already queued,
                    # not already downloaded) — never re-grab one we have or one
                    # that's already downloading.
                    if ek not in needed_keys:
                        continue
                    candidates_by_key.setdefault(ek, []).append(
                        {**g, "parent_dir": parent_dir}
                    )
            else:
                # Reject TV releases that loosely matched the movie title (e.g.
                # "Deep Water" -> "Deep.Water.Salvage.S01..."). A movie folder
                # should never contain a season pack or episode-numbered release,
                # so reject any season/episode marker (bare Sxx, SxxExx, "Season N").
                if _SEASON_OR_EP_RE.search(release_name):
                    log.debug(
                        "poll %s: skip %r — looks like a TV release (season/episode), not a movie",
                        item_id, release_name,
                    )
                    continue
                candidates_by_key.setdefault("movie", []).append(
                    {**g, "parent_dir": parent_dir}
                )

        queued = 0
        # Queue episodes oldest-first (S01E01 before S01E02, season 1 before 2).
        # Keys are zero-padded SxxExx so a plain lexicographic sort is already
        # chronological; the lone "movie" key is unaffected.
        for key in sorted(candidates_by_key):
            candidates = candidates_by_key[key]
            if key in in_queue_keys:
                continue  # already in the AirDC++ queue (bridge- or user-queued)
            # Movies dedup on the completed marker (verified on disk by the
            # fast-skip); TV relies on the live queue check above, so an episode
            # you remove from the queue gets searched & re-grabbed next sweep.
            if kind == "movie" and await state.is_completed(item_id, key):
                continue
            best = max(
                candidates,
                key=lambda g: score_result(
                    g["release_name"], g["total_size"], cfg.quality, item_priority
                ),
            )
            release_name: str = best["release_name"]
            release_hub_path: str = best["parent_dir"]  # e.g. /TV/Drama/<release>

            # Season layer for TV (sonarr's seasonFolderFormat is "Season.{season}"
            # — dot, not space). Movies go straight under the movie root.
            if kind == "tv":
                m_season = re.match(r"S(\d{1,2})E\d", key, re.I)
                season_num = int(m_season.group(1)) if m_season else 0
                release_root_smb = target_base_smb + f"Season.{season_num}\\" + release_name + "\\"
                parent_for_folder = target_base_smb + f"Season.{season_num}\\"
            else:
                release_root_smb = target_base_smb + release_name + "\\"
                parent_for_folder = target_base_smb

            # Whole-folder path: when the hub gave a directory result for this
            # release, queue the entire folder by its id into the PARENT dir.
            # AirDC++ recreates the release folder under the parent, so we pass
            # the parent (not release_root_smb) to avoid a name/name nest.
            if best.get("dir_id"):
                resp = await ad.queue_result(iid, best["dir_id"], parent_for_folder)
                if resp is not None:
                    bid = (resp.get("bundle_info") or {}).get("id")
                    if kind == "movie":  # TV done-ness comes from hasFile/finish, not queue time
                        await state.mark_completed(
                            item_id, key, str(bid) if bid else None, release_name
                        )
                    queued += 1
                    log.info(
                        "queue %s key=%s folder=%r -> %s OK (whole folder)",
                        item_id, key, release_name, parent_for_folder,
                    )
                else:
                    log.warning(
                        "queue %s key=%s folder=%r -> failed",
                        item_id, key, release_name,
                    )
                continue

            # Secondary search by the full release name to capture EVERYTHING
            # in the release folder (the broad show search only returns the
            # top-relevance .r0X parts; sample folders + .sfv usually drop off
            # the per-hub result limit). Then queue every file under the release
            # path, preserving its sub-directory (Sample/, etc.) under the
            # destination so the on-disk layout mirrors the hub layout.
            iid2 = await ad.create_search_instance()
            secondary_files: list[dict] = []
            try:
                if iid2 is not None and await ad.hub_search(iid2, release_name, extensions=None):
                    await asyncio.sleep(8.0)
                    rs2 = await ad.get_results(iid2, 0, 300)
                    for r in rs2:
                        if _is_directory_result(r):
                            continue
                        p = r.get("path") or ""
                        if p.startswith(release_hub_path + _HUB_PATH_SEP) or p == release_hub_path:
                            secondary_files.append(r)
            finally:
                if iid2 is not None:
                    try:
                        await ad.delete_instance(iid2)
                    except Exception:
                        pass

            # If the secondary search didn't return anything (rare), fall back
            # to the files we already grouped from the primary sweep.
            files_to_queue = secondary_files or best["files"]
            log.info(
                "queue %s key=%s release=%r files=%d (primary=%d secondary=%d) root=%s",
                item_id,
                key,
                release_name,
                len(files_to_queue),
                len(best["files"]),
                len(secondary_files),
                release_root_smb,
            )

            queued_files = 0
            last_bundle_id: Optional[str] = None
            seen_tths: set[str] = set()  # in-poll dedup vs the same TTH on multiple hubs
            for f in files_to_queue:
                tth = f.get("tth")
                if not tth or tth in seen_tths:
                    continue
                seen_tths.add(tth)
                # Compute the destination for this specific file: release_root +
                # whatever sub-path the file sits at inside the release folder.
                file_path = f.get("path") or ""
                target_for_file = release_root_smb
                if file_path.startswith(release_hub_path + _HUB_PATH_SEP):
                    sub = file_path[len(release_hub_path) + 1:]
                    sub_dir = sub.rsplit(_HUB_PATH_SEP, 1)[0] if _HUB_PATH_SEP in sub else ""
                    if sub_dir:
                        target_for_file = release_root_smb + sub_dir.replace(_HUB_PATH_SEP, "\\") + "\\"
                resp = await ad.queue_result(iid, tth, target_for_file)
                if resp is not None:
                    queued_files += 1
                    bi = (resp.get("bundle_info") or {}).get("id")
                    if bi:
                        last_bundle_id = str(bi)
            if queued_files:
                if kind == "movie":  # TV done-ness comes from hasFile/finish, not queue time
                    await state.mark_completed(
                        item_id, key, last_bundle_id, release_name
                    )
                queued += 1
                log.info(
                    "queue %s key=%s OK (%d files queued)",
                    item_id,
                    key,
                    queued_files,
                )

        if queued:
            log.info("poll %s: queued %d new key(s) total", item_id, queued)
            # If a queued release already exists on disk, AirDC++ instant-completes
            # it without leaving a bundle to catch — so nudge a targeted *arr rescan
            # so it imports them (hasFile=true) and they leave the wanted set,
            # instead of being re-queued every sweep.
            if kind == "tv":
                await trigger_arr_rescan(cfg, item_id)
    finally:
        try:
            await ad.delete_instance(iid)
        except Exception:
            log.debug("delete_instance %s failed (ignored)", iid)

    # We issued an AirDC++ search round-trip for this item; the poller uses this
    # to spend its inter-search jitter only on real searches.
    return True


# ── Entrypoint ───────────────────────────────────────────────────────────────


