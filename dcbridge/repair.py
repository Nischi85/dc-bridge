"""Human-triggered repair for a release whose SFV integrity check found
missing volumes (media-audit's sfv_integrity check) — searches the hub for
a source still serving the EXACT same release and downloads just the
missing files back into the existing folder, rather than re-grabbing the
whole release. Deliberately not automatic: unlike the rest of dc-bridge's
poller, nothing calls this on its own; it only runs when a human clicks
"repair" on a specific finding (see media-audit's UI), because downloading
based on a filename match carries real risk if the match is wrong — a RAR
volume from a different release, even one that looks similar by name,
corrupts the whole archive rather than fixing it.

Identity check is deliberately the strictest thing in this codebase: a hub
result is only trusted if its OWN containing release-folder name is an
EXACT (case-insensitive) match for the broken release's real folder name —
never a fuzzy/partial title match like the rest of the poller uses for
ordinary searches. The whole point of scene release naming plus its own
.sfv manifest is that an identically-named release is the same bytes; a
same-song-different-release match here would be exactly the failure mode
this exists to avoid, not something a "close enough" match should risk.

Shares the same AirDCPP client every other search goes through, so it's
covered by the same global hub_search rate limiter (see AirDCPPCfg.
min_search_interval_seconds) without any extra wiring.
"""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from typing import Any

from dcbridge.airdcpp import AirDCPP
from dcbridge.config import Config
from dcbridge.helpers import loosen_hyphens_for_search, sanitize_for_dc_search
from dcbridge.util import _HUB_PATH_SEP, _is_directory_result, _parent_dir_and_name, _to_smb_dir

log = logging.getLogger("dc_bridge")

_SEARCH_SETTLE_SECONDS = 8.0


async def repair_release(
    cfg: Config, ad: AirDCPP, release_dir_fs: str, missing_files: list[str],
    settle_seconds: float = _SEARCH_SETTLE_SECONDS,
) -> dict[str, Any]:
    """Returns {"ok": bool, "queued": [filenames], "not_found": [filenames],
    "error": str | None}. Never raises — a hub_search/queue failure is
    reported in the result, not an exception, since this runs from an HTTP
    handler a human is watching for the outcome.

    settle_seconds is a parameter (not just the module constant) purely so
    tests can pass 0 — real callers always get the full settle window."""
    release_name = Path(release_dir_fs).name
    if not release_name or not missing_files:
        return {"ok": False, "error": "nothing to do", "queued": [], "not_found": list(missing_files)}

    query = loosen_hyphens_for_search(sanitize_for_dc_search(release_name))
    iid = await ad.create_search_instance()
    if iid is None:
        return {
            "ok": False, "error": "could not create search instance",
            "queued": [], "not_found": list(missing_files),
        }

    try:
        if not await ad.hub_search(iid, query, extensions=None):
            return {"ok": False, "error": "hub search failed", "queued": [], "not_found": list(missing_files)}
        await asyncio.sleep(settle_seconds)
        results = await ad.get_results(iid, 0, 500)

        wanted = {f.lower() for f in missing_files}
        found_by_name: dict[str, dict] = {}
        for r in results:
            if _is_directory_result(r):
                continue
            path = r.get("path") or ""
            _parent, folder_name = _parent_dir_and_name(path)
            if not folder_name or folder_name.lower() != release_name.lower():
                continue
            fname = path.rsplit(_HUB_PATH_SEP, 1)[-1]
            if fname.lower() in wanted and fname.lower() not in found_by_name:
                found_by_name[fname.lower()] = r

        target_smb = _to_smb_dir(release_dir_fs, cfg.path_map)
        queued: list[str] = []
        not_found: list[str] = []
        for fname in missing_files:
            r = found_by_name.get(fname.lower())
            tth = r.get("tth") if r else None
            if not tth:
                not_found.append(fname)
                continue
            queue_result = await ad.queue_result(iid, tth, target_smb)
            if queue_result is not None:
                queued.append(fname)
            else:
                not_found.append(fname)

        log.info(
            "repair %r: %d/%d file(s) queued from an exact-name hub match, %d not found",
            release_name, len(queued), len(missing_files), len(not_found),
        )
        return {"ok": True, "error": None, "queued": queued, "not_found": not_found}
    finally:
        try:
            await ad.delete_instance(iid)
        except Exception:
            log.debug("repair: delete_instance %s failed (ignored)", iid)
