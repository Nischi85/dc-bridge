"""Tests for movie_completion's on-disk completeness cross-check. Regression
test for the Underworld: Evolution incident (2026-08-22): AirDC++ reported a
movie's grab "completed" with only 2 of 87 expected RAR volumes actually on
disk — but one of the two present was the LAST volume (.rar), which is all
the old _release_complete-only check looked for. The SFV manifest cross-check
(movie_completion now prefers it, reading the real backing storage directly)
is what actually catches this; the AirDC++ list_dir fallback path is only
exercised when no readable .sfv is found there.
"""
import asyncio

from dcbridge.config import Config, PathMap, PollerCfg
from dcbridge.poller import movie_completion


class FakeAirDCPP:
    def __init__(self, dir_items):
        self._dir_items = dir_items

    async def list_dir(self, smb_path):
        return self._dir_items


def _cfg(linux_root="/mnt/user/media/verified", download_grace_hours=24):
    return Config.model_construct(
        path_map=PathMap(linux_root=str(linux_root), windows_root="Z:"),
        poller=PollerCfg(download_grace_hours=download_grace_hours),
    )


def _item(target_dir_fs):
    return {"id": "radarr:4315", "title": "Underworld: Evolution", "target_dir_fs": str(target_dir_fs)}


def test_pre_existing_marker_is_always_complete():
    result = asyncio.run(
        movie_completion(FakeAirDCPP(None), _cfg(), _item("/x"), "(pre-existing)", 1000, 2000)
    )
    assert result == "complete"


def test_underworld_evolution_incident_last_volume_only_is_not_complete(tmp_path):
    release = "Underworld.Evolution.2006.iNTERNAL.720p.BluRay.x264-RECLUSE"
    release_dir = tmp_path / release
    release_dir.mkdir()
    all_volumes = [f"{release.lower()}.r{i:02d}" for i in range(87)] + [f"{release.lower()}.rar"]
    sfv = release_dir / f"{release.lower()}.sfv"
    sfv.write_text("".join(f"{v} DEADBEEF\n" for v in all_volumes), encoding="utf-8")
    (release_dir / f"{release.lower()}.r00").write_bytes(b"x")
    (release_dir / f"{release.lower()}.rar").write_bytes(b"x")  # last volume present...

    # AirDC++'s own view would wrongly call it done (only checks for .rar) —
    # confirms the SFV check, not the fallback, is what's catching this.
    ad = FakeAirDCPP([{"name": f"{release.lower()}.rar", "type": {"id": "file"}}])
    now_ts = 1000 + 3600  # well within the grace window
    result = asyncio.run(
        movie_completion(ad, _cfg(), _item(tmp_path), release, 1000, now_ts)
    )
    assert result == "downloading"  # not complete, but still within grace — not stalled either


def test_sfv_verified_complete_release_is_complete(tmp_path):
    release = "Underworld.Evolution.2006.DVD9.1080p.BluRay.x264-hV"
    release_dir = tmp_path / release
    release_dir.mkdir()
    all_volumes = [f"{release.lower()}.r{i:02d}" for i in range(3)] + [f"{release.lower()}.rar"]
    sfv = release_dir / f"{release.lower()}.sfv"
    sfv.write_text("".join(f"{v} DEADBEEF\n" for v in all_volumes), encoding="utf-8")
    for v in all_volumes:
        (release_dir / v).write_bytes(b"x")

    ad = FakeAirDCPP(None)  # never consulted — the SFV check wins outright
    result = asyncio.run(
        movie_completion(ad, _cfg(), _item(tmp_path), release, 1000, 2000)
    )
    assert result == "complete"


def test_no_sfv_falls_back_to_airdcpp_list_dir_check(tmp_path):
    # No .sfv on disk at all (e.g. path not yet visible, or a non-RAR
    # release) — behavior must match the pre-existing AirDC++-based check.
    release = "SomeMovie.2020.WEB-GROUP"
    ad = FakeAirDCPP([{"name": f"{release.lower()}.mkv", "type": {"id": "file"}}])
    result = asyncio.run(
        movie_completion(ad, _cfg(linux_root=tmp_path), _item(tmp_path), release, 1000, 2000)
    )
    assert result == "complete"


def test_stalled_past_grace_window_when_nothing_verifies(tmp_path):
    release = "Ghost.Release.2020.WEB-GROUP"
    ad = FakeAirDCPP([])
    now_ts = 1000 + int(25 * 3600)  # past the 24h default grace
    result = asyncio.run(
        movie_completion(ad, _cfg(linux_root=tmp_path), _item(tmp_path), release, 1000, now_ts)
    )
    assert result == "stalled"
