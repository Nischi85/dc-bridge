"""Human-triggered repair for a release whose SFV integrity check found
missing volumes (media-audit's sfv_integrity check) — searches the hub for
a source still serving the EXACT same release and re-queues the WHOLE
folder to its existing location, the same way every other grab in this
codebase already works (poller.py's _queue_candidates queues a directory
result by its own `id`, not file-by-file). Deliberately not automatic:
unlike the rest of dc-bridge's poller, nothing calls this on its own; it
only runs when a human clicks "repair" on a specific finding (see
media-audit's UI), because a wrong hub match — a RAR volume from a
different release, even one that looks similar by name — corrupts the
whole archive rather than fixing it.

Why whole-folder, not per-file (learned live, 2026-08-28): downloading an
individual already-present FILE result via /search/{id}/results/{tth}/
download fails outright — AirDC++ returned 400 "File exists on the disk
already" for every one of 99 genuinely-missing files, because the search
result for the whole folder itself carried `"dupe": {"id": "share_full"}` —
AirDC++'s own share index believes this exact path is already a complete
duplicate of what it has, and that belief blocks per-file downloads
targeting it even for files that plainly aren't there. A share refresh of
the path didn't clear it either. Queueing the FOLDER instead sidesteps
this: per the person actually running this hub day to day, queueing a
whole release AirDC++ considers "already available" is normal and
expected — files it already has are reported informational, not a
failure, and files that are genuinely missing still get downloaded. This
is exactly the "download only what's missing" outcome repair is for; it
just turns out AirDC++ already does that itself at the folder level, and
fighting its per-file dupe belief for the same behavior was the wrong
layer to work at.

Identity check is deliberately the strictest thing in this codebase: a hub
result is only trusted if its OWN folder name is an EXACT (case-
insensitive) match for the broken release's real folder name — never a
fuzzy/partial title match like the rest of the poller uses for ordinary
searches. The whole point of scene release naming plus its own .sfv
manifest is that an identically-named release is the same bytes; a
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
from dcbridge.util import _HUB_PATH_SEP, _is_directory_result, _to_smb_dir

log = logging.getLogger("dc_bridge")

_SEARCH_SETTLE_SECONDS = 8.0


def _directory_folder_name(path: str) -> str:
    return path.rstrip("/").rsplit(_HUB_PATH_SEP, 1)[-1]


async def repair_release(
    cfg: Config, ad: AirDCPP, release_dir_fs: str, missing_files: list[str],
    settle_seconds: float = _SEARCH_SETTLE_SECONDS,
) -> dict[str, Any]:
    """Returns {"ok": bool, "queued": [filenames], "not_found": [filenames],
    "error": str | None}. "queued" here means the whole-folder re-queue was
    ACCEPTED by AirDC++, not that every file has finished downloading — same
    as any other grab, check dc-bridge/AirDC++'s own queue for progress.
    Never raises — a hub_search/queue failure is reported in the result, not
    an exception, since this runs from an HTTP handler a human is watching
    for the outcome.

    settle_seconds is a parameter (not just the module constant) purely so
    tests can pass 0 — real callers always get the full settle window."""
    release_name = Path(release_dir_fs).name
    if not release_name or not missing_files:
        return {"ok": False, "error": "nothing to do", "queued": [], "not_found": list(missing_files)}

    # Resolve the SMB target BEFORE ever touching AirDC++ — release_dir_fs
    # not being under cfg.path_map.linux_root is a caller bug (the exact
    # thing that crashed this endpoint in production before this check
    # existed: media-audit sent its own container path, not dc-bridge's),
    # not something worth a wasted hub search to discover.
    #
    # The target must be the release's PARENT directory, not the release
    # folder itself — AirDC++ appends the directory result's own folder name
    # to whatever target it's given (same convention as poller.py's
    # parent_for_folder for an ordinary grab). Passing the release folder
    # itself made AirDC++ create a full nested duplicate of the release
    # INSIDE the existing release folder instead of merging into it — caught
    # live 2026-08-28 on two real repairs (Jesse Stone, Lilo & Stitch), each
    # left with a byte-exact duplicate copy of the whole release nested one
    # level deeper.
    try:
        target_smb = _to_smb_dir(str(Path(release_dir_fs).parent), cfg.path_map)
    except ValueError as e:
        return {"ok": False, "error": f"path translation failed: {e}", "queued": [], "not_found": list(missing_files)}

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

        dir_result = None
        for r in results:
            if not _is_directory_result(r):
                continue
            if _directory_folder_name(r.get("path") or "").lower() == release_name.lower():
                dir_result = r
                break

        if dir_result is None:
            log.info("repair %r: no exact-name directory match on the hub", release_name)
            return {
                "ok": True, "error": "no exact-name hub source found",
                "queued": [], "not_found": list(missing_files),
            }

        result_id = dir_result.get("id")
        if not result_id or await ad.queue_result(iid, result_id, target_smb) is None:
            return {
                "ok": False, "error": "queueing the release folder failed",
                "queued": [], "not_found": list(missing_files),
            }

        log.info(
            "repair %r: whole-folder re-queue accepted (%d file(s) were missing before this) — "
            "AirDC++ will skip what it already has and fetch the rest",
            release_name, len(missing_files),
        )
        return {"ok": True, "error": None, "queued": list(missing_files), "not_found": []}
    finally:
        try:
            await ad.delete_instance(iid)
        except Exception:
            log.debug("repair: delete_instance %s failed (ignored)", iid)
