"""Tests for remove_finished_tv_bundles's on-disk completeness cross-check.

Regression test for the Outer Banks S05E04 incident: AirDC++ reported a TV
bundle "completed" (status.completed=True) while the base .rar volume never
actually arrived — only .r00/.r01/... parts were present. The old code
trusted the flag blindly and called mark_completed, permanently blocking
re-search since Sonarr's own hasFile stayed false and nothing else caught
the gap. These tests run the async function directly via asyncio.run (no
pytest-asyncio dependency needed for one small test module).
"""
import asyncio

from dcbridge.config import ArrCfg, Config, PathMap
from dcbridge.poller import remove_finished_tv_bundles


class FakeState:
    def __init__(self):
        self.completed: list[tuple] = []
        self.failed: list[tuple] = []

    async def mark_completed(self, item_id, key, size, name):
        self.completed.append((item_id, key, size, name))

    async def add_failed_release(self, item_id, key, release_name):
        self.failed.append((item_id, key, release_name))


class FakeAirDCPP:
    def __init__(self, dir_items):
        self._dir_items = dir_items
        self.removed_bundle_ids: list[int] = []

    async def list_dir(self, smb_path):
        return self._dir_items

    async def remove_bundle(self, bundle_id, remove_finished=False):
        self.removed_bundle_ids.append(bundle_id)
        return True


def _cfg():
    # sonarr.api_key left blank so trigger_arr_rescan's `if not (key and ...)`
    # guard no-ops it — this test is about the completeness check, not *arr.
    return Config.model_construct(
        path_map=PathMap(linux_root="/mnt/user/media/verified", windows_root="Z:"),
        sonarr=ArrCfg(url="http://sonarr:8989"),
    )


def _item():
    return {"id": "sonarr:663", "title": "Outer Banks", "target_dir_fs": "/mnt/user/media/verified/Outer Banks"}


def _bundle(name="Outer.Banks.S05E04.1080p.WEB.H264-CAKES"):
    return {"id": 42, "name": name, "status": {"completed": True}}


def test_completed_bundle_missing_rar_is_not_marked_done_but_recorded_failed():
    # Only .r00/.r01 parts present, no .rar and no video file — this is the
    # exact Outer Banks S05E04 shape.
    dir_items = [
        {"name": "Outer.Banks.S05E04.1080p.WEB.H264-CAKES.r00", "type": {"id": "file"}},
        {"name": "Outer.Banks.S05E04.1080p.WEB.H264-CAKES.r01", "type": {"id": "file"}},
    ]
    ad = FakeAirDCPP(dir_items)
    state = FakeState()
    suspect_count = asyncio.run(
        remove_finished_tv_bundles(ad, state, _cfg(), _item(), [_bundle()])
    )
    assert suspect_count == 1
    assert state.completed == []
    assert state.failed == [("sonarr:663", "S05E04", "Outer.Banks.S05E04.1080p.WEB.H264-CAKES")]
    assert ad.removed_bundle_ids == [42]


def test_completed_bundle_with_rar_present_is_marked_done_normally():
    dir_items = [
        {"name": "Outer.Banks.S05E04.1080p.WEB.H264-CAKES.r00", "type": {"id": "file"}},
        {"name": "Outer.Banks.S05E04.1080p.WEB.H264-CAKES.rar", "type": {"id": "file"}},
    ]
    ad = FakeAirDCPP(dir_items)
    state = FakeState()
    suspect_count = asyncio.run(
        remove_finished_tv_bundles(ad, state, _cfg(), _item(), [_bundle()])
    )
    assert suspect_count == 0
    assert state.failed == []
    assert len(state.completed) == 1
    assert state.completed[0][:2] == ("sonarr:663", "S05E04")


def test_unverifiable_dir_listing_falls_back_to_trusting_the_completed_flag():
    # list_dir returning None means "couldn't check" (transient webapi issue),
    # not "confirmed missing" — must not block completion on uncertainty.
    ad = FakeAirDCPP(None)
    state = FakeState()
    suspect_count = asyncio.run(
        remove_finished_tv_bundles(ad, state, _cfg(), _item(), [_bundle()])
    )
    assert suspect_count == 0
    assert state.failed == []
    assert len(state.completed) == 1
