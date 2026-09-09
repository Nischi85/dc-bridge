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
        self._markers = list(markers)
        self._verified: set[str] = set()
        self.rescans: list[str] = []
        self.cleared: list[str] = []
        self.failed: list[tuple] = []
        self.backlog = None

    async def get_completed_keys(self, item_id):
        return [(k, r, q) for k, r, q in self._markers if k not in self.cleared]

    async def is_completed(self, item_id, key):
        return key in self._verified

    async def mark_completed(self, item_id, key, bundle_id, release_name):
        self._verified.add(key)

    async def clear_completed(self, item_id, key):
        self.cleared.append(key)

    async def add_failed_release(self, item_id, key, release_name):
        self.failed.append((key, release_name))

    async def set_search_backlog(self, item_id, n):
        self.backlog = n


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


def _item(target_dir_fs="/nonexistent/Outer Banks"):
    return {"id": "sonarr:663", "title": "Outer Banks", "target_dir_fs": target_dir_fs}


def _run(cfg, state, item, now_ts, sonarr_have, monkeypatch):
    calls = {"rescan": 0}

    async def fake_sonarr_imported_episode_keys(cfg_, item_id):
        return sonarr_have

    async def fake_trigger_arr_rescan(cfg_, item_id):
        calls["rescan"] += 1

    monkeypatch.setattr(poller, "sonarr_imported_episode_keys", fake_sonarr_imported_episode_keys)
    monkeypatch.setattr(poller, "trigger_arr_rescan", fake_trigger_arr_rescan)
    calls["invalidated"] = asyncio.run(poller.verify_tv_imports(cfg, state, item, now_ts))
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


# ── on-disk completeness check (the Reacher S04E02 class) ──────────────────

def _seasondir(tmp_path, season):
    d = tmp_path / f"Season.{season}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_incomplete_grab_on_disk_is_dropped_and_blocklisted(monkeypatch, tmp_path):
    # Reacher S04E02: .r00-.r10 present but NO .rar first volume and NO .sfv.
    rel = "Reacher.S04E02.1080p.WEB.H264-CAKES"
    d = _seasondir(tmp_path, 4) / rel
    d.mkdir()
    for i in range(11):
        (d / f"x.r{i:02d}").write_bytes(b"x")
    (d / "x.nfo").write_bytes(b"x")

    cfg = _cfg(interval_seconds=900)
    state = FakeState([("S04E02", rel, 1000)])
    now_ts = 1000 + 1800 + 1
    calls = _run(cfg, state, _item(str(tmp_path)), now_ts=now_ts, sonarr_have=set(), monkeypatch=monkeypatch)

    assert "S04E02" in state.cleared            # marker dropped
    assert (("S04E02", rel) in state.failed)    # release blocklisted
    assert calls["invalidated"] == 1            # caller folds this into search_backlog
    assert calls["rescan"] == 0                 # no pointless rescan for a dead grab
    assert "S04E02:verified" not in state._verified


def test_complete_grab_on_disk_still_rescans_when_sonarr_lacks_it(monkeypatch, tmp_path):
    # Files are all there (has the .rar) — Sonarr's own import failed, so a
    # rescan is still the right nudge; do NOT drop the marker.
    rel = "Reacher.S04E07.1080p.WEB.H264-CAKES"
    d = _seasondir(tmp_path, 4) / rel
    d.mkdir()
    (d / "x.rar").write_bytes(b"x")
    (d / "x.mkv").write_bytes(b"x")

    cfg = _cfg(interval_seconds=900)
    state = FakeState([("S04E07", rel, 1000)])
    now_ts = 1000 + 1800 + 1
    calls = _run(cfg, state, _item(str(tmp_path)), now_ts=now_ts, sonarr_have=set(), monkeypatch=monkeypatch)

    assert state.cleared == [] and state.failed == []
    assert calls["rescan"] == 1


def test_missing_release_folder_is_treated_as_a_dead_grab(monkeypatch, tmp_path):
    _seasondir(tmp_path, 4)  # season dir exists, the release folder does not
    rel = "Reacher.S04E02.1080p.WEB.H264-CAKES"
    cfg = _cfg(interval_seconds=900)
    state = FakeState([("S04E02", rel, 1000)])
    now_ts = 1000 + 1800 + 1
    calls = _run(cfg, state, _item(str(tmp_path)), now_ts=now_ts, sonarr_have=set(), monkeypatch=monkeypatch)

    assert "S04E02" in state.cleared and ("S04E02", rel) in state.failed
    assert calls["rescan"] == 0


def test_sfv_verifies_complete_does_not_drop_marker(monkeypatch, tmp_path):
    rel = "Reacher.S04E02.1080p.WEB.H264-CAKES"
    d = _seasondir(tmp_path, 4) / rel
    d.mkdir()
    names = [f"x.r{i:02d}" for i in range(3)] + ["x.rar"]
    (d / "x.sfv").write_text("".join(f"{n} DEADBEEF\n" for n in names), encoding="utf-8")
    for n in names:
        (d / n).write_bytes(b"x")

    cfg = _cfg(interval_seconds=900)
    state = FakeState([("S04E02", rel, 1000)])
    now_ts = 1000 + 1800 + 1
    calls = _run(cfg, state, _item(str(tmp_path)), now_ts=now_ts, sonarr_have=set(), monkeypatch=monkeypatch)
    assert state.cleared == [] and calls["rescan"] == 1
