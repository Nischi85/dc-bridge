"""Tests for build_alert_state — the pure snapshot builder alert_loop writes to
alert_file. Dedup/notification-firing lives in the host-side alert-notify.py
script, not here; this just has to report state truthfully."""
from dcbridge.alerting import build_alert_state


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
