"""Tests for build_alert_state — the pure snapshot builder alert_loop writes to
alert_file. Dedup/notification-firing lives in the host-side alert-notify.py
script, not here; this just has to report state truthfully.

Also covers _read_rargate_stuck_count's stuck_seconds filtering — the fix
for a real false alarm (2026-09-14): rargate adds a release to
stuck_releases the moment SFV validation fails 3 times, which happened
~20-30s into two perfectly healthy in-progress Harry Potter downloads."""
import json

from dcbridge.alerting import _read_rargate_stuck_count, build_alert_state


def test_no_issues_when_everything_is_healthy():
    snapshot = build_alert_state(0, 0, 1_000)
    assert snapshot["issues"] == []


def test_stale_tracking_produces_one_issue():
    snapshot = build_alert_state(3, 0, 1_000)
    ids = {i["id"] for i in snapshot["issues"]}
    assert ids == {"dc_bridge_stale_tracking"}
    assert "3 active item" in snapshot["issues"][0]["description"]


def test_rargate_stuck_releases_produces_one_issue():
    snapshot = build_alert_state(0, 2, 1_000)
    ids = {i["id"] for i in snapshot["issues"]}
    assert ids == {"rargate_stuck_releases"}


def test_rargate_none_means_unknown_not_healthy_and_is_not_reported_as_an_issue():
    # rargate_stuck_count=None means "couldn't read the status file" — not
    # configured, or rargate is down. That's not itself something to alert on
    # here (a missing/unreadable status file isn't proof of a stuck release);
    # it should simply not add an issue, same as a real zero would.
    snapshot = build_alert_state(0, None, 1_000)
    assert snapshot["issues"] == []


def test_both_problems_produce_two_issues():
    snapshot = build_alert_state(1, 1, 1_000)
    ids = {i["id"] for i in snapshot["issues"]}
    assert ids == {"dc_bridge_stale_tracking", "rargate_stuck_releases"}


def test_checked_at_is_passed_through():
    snapshot = build_alert_state(0, 0, 12345)
    assert snapshot["checked_at"] == 12345


# ── _read_rargate_stuck_count ────────────────────────────────────────────

def _write_status(tmp_path, stuck_releases):
    p = tmp_path / "rargate-status.json"
    p.write_text(json.dumps({"stuck_releases_count": len(stuck_releases), "stuck_releases": stuck_releases}))
    return str(p)


def test_a_release_stuck_only_a_few_seconds_is_not_counted(tmp_path):
    # The exact false-alarm shape: two Harry Potter releases each tripped
    # rargate's 3-failed-SFV-checks threshold 20-30s into a healthy download.
    status_file = _write_status(tmp_path, [
        {"path": "/x/Half.Blood.Prince", "stuck_seconds": 20, "failure_count": 3},
        {"path": "/x/Deathly.Hallows.Part.2", "stuck_seconds": 27, "failure_count": 3},
    ])
    assert _read_rargate_stuck_count(status_file, min_stuck_seconds=1800) == 0


def test_a_release_stuck_past_the_threshold_is_counted(tmp_path):
    status_file = _write_status(tmp_path, [
        {"path": "/x/Really.Dead.Release", "stuck_seconds": 3700, "failure_count": 50},
    ])
    assert _read_rargate_stuck_count(status_file, min_stuck_seconds=1800) == 1


def test_mixed_only_the_genuinely_stuck_ones_are_counted(tmp_path):
    status_file = _write_status(tmp_path, [
        {"path": "/x/Fresh.Download", "stuck_seconds": 15, "failure_count": 3},
        {"path": "/x/Really.Dead.Release", "stuck_seconds": 3700, "failure_count": 50},
    ])
    assert _read_rargate_stuck_count(status_file, min_stuck_seconds=1800) == 1


def test_default_threshold_zero_keeps_the_old_raw_count_behavior(tmp_path):
    status_file = _write_status(tmp_path, [{"path": "/x/Fresh", "stuck_seconds": 5, "failure_count": 3}])
    assert _read_rargate_stuck_count(status_file) == 1


def test_missing_status_file_returns_none_not_zero(tmp_path):
    assert _read_rargate_stuck_count(str(tmp_path / "nope.json"), min_stuck_seconds=1800) is None


def test_empty_status_file_path_returns_none():
    assert _read_rargate_stuck_count("", min_stuck_seconds=1800) is None


def test_no_stuck_releases_list_falls_back_to_the_raw_count(tmp_path):
    # An older/unexpected status shape with no per-release list at all —
    # don't go blind on this field, just use the raw count as before.
    p = tmp_path / "rargate-status.json"
    p.write_text(json.dumps({"stuck_releases_count": 2}))
    assert _read_rargate_stuck_count(str(p), min_stuck_seconds=1800) == 2
