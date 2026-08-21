"""Tests for verify_tv_imports — the per-episode safety net that confirms
Sonarr actually imported a completed bridge grab, not just that a rescan was
*requested*. Regression test for a real incident: AirDC++ finished a grab,
dc-bridge marked it complete and fired its one-shot rescan, but Sonarr's own
file-matcher silently failed to import that one file (every sibling episode
with the identical naming pattern imported fine) — nothing ever retried it
since remove_finished_tv_bundles only rescans once, when the bundle first
disappears from AirDC++'s queue.
"""
import asyncio

import dcbridge.poller as poller


class FakeState:
    def __init__(self, markers):
        # markers: list of (key, release_name, queued_at)
        self._markers = markers
        self._verified: set[str] = set()
        self.rescans: list[str] = []

    async def get_completed_keys(self, item_id):
        return list(self._markers)

    async def is_completed(self, item_id, key):
        return key in self._verified

    async def mark_completed(self, item_id, key, bundle_id, release_name):
        self._verified.add(key)


def _cfg(interval_seconds=900):
    class _PollerCfg:
        pass

    class _Cfg:
        pass

    pc = _PollerCfg()
    pc.interval_seconds = interval_seconds
    c = _Cfg()
    c.poller = pc
    return c


def _item():
    return {"id": "sonarr:663", "title": "Outer Banks"}


def _run(cfg, state, item, now_ts, sonarr_have, monkeypatch):
    calls = {"rescan": 0}

    async def fake_sonarr_imported_episode_keys(cfg_, item_id):
        return sonarr_have

    async def fake_trigger_arr_rescan(cfg_, item_id):
        calls["rescan"] += 1

    monkeypatch.setattr(poller, "sonarr_imported_episode_keys", fake_sonarr_imported_episode_keys)
    monkeypatch.setattr(poller, "trigger_arr_rescan", fake_trigger_arr_rescan)
    asyncio.run(poller.verify_tv_imports(cfg, state, item, now_ts))
    return calls


def test_no_completed_keys_does_nothing(monkeypatch):
    state = FakeState([])
    calls = _run(_cfg(), state, _item(), now_ts=10_000, sonarr_have=set(), monkeypatch=monkeypatch)
    assert calls["rescan"] == 0
    assert state._verified == set()


def test_pre_existing_markers_are_ignored(monkeypatch):
    state = FakeState([("S05E01", "(pre-existing)", 1000)])
    calls = _run(_cfg(), state, _item(), now_ts=10_000, sonarr_have=set(), monkeypatch=monkeypatch)
    assert calls["rescan"] == 0
    assert state._verified == set()


def test_already_verified_key_is_not_rechecked(monkeypatch):
    state = FakeState([("S05E04", "Outer.Banks.S05E04-CAKES", 1000)])
    state._verified.add("S05E04:verified")
    calls = _run(_cfg(), state, _item(), now_ts=1_000_000, sonarr_have=set(), monkeypatch=monkeypatch)
    assert calls["rescan"] == 0


def test_within_grace_period_does_not_check_sonarr_yet(monkeypatch):
    cfg = _cfg(interval_seconds=900)  # grace = max(1800, 1800) = 1800s
    state = FakeState([("S05E04", "Outer.Banks.S05E04-CAKES", 1000)])
    calls = _run(cfg, state, _item(), now_ts=1500, sonarr_have=None, monkeypatch=monkeypatch)
    assert calls["rescan"] == 0
    assert state._verified == set()  # never got far enough to verify


def test_past_grace_and_sonarr_confirms_import_marks_verified_no_rescan(monkeypatch):
    cfg = _cfg(interval_seconds=900)
    state = FakeState([("S05E04", "Outer.Banks.S05E04-CAKES", 1000)])
    now_ts = 1000 + 1800 + 1
    calls = _run(cfg, state, _item(), now_ts=now_ts, sonarr_have={"S05E04"}, monkeypatch=monkeypatch)
    assert calls["rescan"] == 0
    assert "S05E04:verified" in state._verified


def test_past_grace_and_sonarr_still_missing_retries_rescan_not_verified(monkeypatch):
    # The actual regression case: Sonarr never imported it.
    cfg = _cfg(interval_seconds=900)
    state = FakeState([("S05E04", "Outer.Banks.S05E04-CAKES", 1000)])
    now_ts = 1000 + 1800 + 1
    calls = _run(cfg, state, _item(), now_ts=now_ts, sonarr_have=set(), monkeypatch=monkeypatch)
    assert calls["rescan"] == 1
    assert "S05E04:verified" not in state._verified


def test_sonarr_unreachable_does_not_rescan_or_verify(monkeypatch):
    cfg = _cfg(interval_seconds=900)
    state = FakeState([("S05E04", "Outer.Banks.S05E04-CAKES", 1000)])
    now_ts = 1000 + 1800 + 1
    calls = _run(cfg, state, _item(), now_ts=now_ts, sonarr_have=None, monkeypatch=monkeypatch)
    assert calls["rescan"] == 0
    assert state._verified == set()


def test_mixed_episodes_some_verified_some_still_missing(monkeypatch):
    cfg = _cfg(interval_seconds=900)
    state = FakeState([
        ("S05E01", "Outer.Banks.S05E01-CAKES", 1000),
        ("S05E04", "Outer.Banks.S05E04-CAKES", 1000),
    ])
    now_ts = 1000 + 1800 + 1
    calls = _run(cfg, state, _item(), now_ts=now_ts, sonarr_have={"S05E01"}, monkeypatch=monkeypatch)
    assert calls["rescan"] == 1  # S05E04 still missing
    assert "S05E01:verified" in state._verified
    assert "S05E04:verified" not in state._verified
