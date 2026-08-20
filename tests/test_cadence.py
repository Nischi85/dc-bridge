"""Tests for compute_cadence — the scheduling state machine shared by poll_item
(does it search this sweep?) and the schedule report (when next?).

This is the trickiest logic in the bridge by its own docstrings: availability
gating, the "always probe a fresh request once" rule, content-age back-off, the
fresh-episode override that caps (never slows) that back-off, and backlog
draining that must override everything else. Each test below targets one of
those interacting rules in isolation.
"""
from dcbridge.config import (
    AirDCPPCfg,
    ArrCfg,
    BackoffTier,
    Config,
    MatchCfg,
    PathMap,
    PollerCfg,
    QualityCfg,
)
from dcbridge.helpers import compute_cadence, _utc_iso

NOW = 1_800_000_000  # arbitrary fixed reference epoch


def _cfg(*, backoff=None, movie_release_offset_days=0.0,
          fresh_episode_hours=48.0, fresh_episode_every_seconds=7200) -> Config:
    return Config(
        airdcpp=AirDCPPCfg(url="http://x", username="u", password="p"),
        sonarr=ArrCfg(url="http://sonarr"),
        radarr=ArrCfg(url="http://radarr"),
        path_map=PathMap(linux_root="/x", windows_root="Z:\\x"),
        quality=QualityCfg(episode_size_mb=(100, 5000), movie_size_mb=(500, 20000)),
        poller=PollerCfg(
            backoff=backoff or [],
            fresh_episode_hours=fresh_episode_hours,
            fresh_episode_every_seconds=fresh_episode_every_seconds,
        ),
        match=MatchCfg(movie_release_offset_days=movie_release_offset_days),
    )


def _tv_item(**overrides) -> dict:
    base = {"kind": "tv", "last_searched_at": 0, "request_created_at": 0,
            "search_backlog": 0, "air_anchor_utc": None, "next_air_utc": None}
    base.update(overrides)
    return base


def _movie_item(**overrides) -> dict:
    base = {"kind": "movie", "last_searched_at": 0, "request_created_at": 0,
            "search_backlog": 0, "release_date_utc": None, "year": None}
    base.update(overrides)
    return base


# ── fresh-request "initial" probe always wins ────────────────────────────────


def test_fresh_request_is_due_even_before_tv_air_date():
    item = _tv_item(next_air_utc=_utc_iso(NOW + 86400), request_created_at=NOW)
    d = compute_cadence(item, _cfg(), NOW)
    assert d["due"] is True and d["status"] == "initial"


def test_fresh_request_is_due_even_before_movie_release_date():
    item = _movie_item(release_date_utc=_utc_iso(NOW + 86400), request_created_at=NOW)
    d = compute_cadence(item, _cfg(), NOW)
    assert d["due"] is True and d["status"] == "initial"


def test_never_searched_counts_as_initial_even_without_request_created_at():
    item = _tv_item(last_searched_at=0, request_created_at=0)
    d = compute_cadence(item, _cfg(), NOW)
    assert d["due"] is True and d["status"] == "initial"


def test_a_search_after_request_created_is_no_longer_initial():
    item = _tv_item(last_searched_at=NOW - 10, request_created_at=NOW - 100,
                     air_anchor_utc=None, next_air_utc=None)
    d = compute_cadence(item, _cfg(), NOW)
    assert d["status"] != "initial"


# ── TV availability gate ─────────────────────────────────────────────────────


def test_tv_nothing_wanted_and_searched_before_is_complete():
    item = _tv_item(last_searched_at=NOW - 10, request_created_at=NOW - 100)
    d = compute_cadence(item, _cfg(), NOW)
    assert d == {"due": False, "status": "complete", "next_due": None,
                 "detail": "no episode wanted"}


def test_tv_gated_before_next_air_date():
    item = _tv_item(next_air_utc=_utc_iso(NOW + 3600),
                     last_searched_at=NOW - 10, request_created_at=NOW - 100)
    d = compute_cadence(item, _cfg(), NOW)
    assert d["due"] is False and d["status"] == "gated"
    assert d["next_due"] == NOW + 3600


def test_tv_due_the_moment_an_episode_aired_since_last_check():
    item = _tv_item(air_anchor_utc=_utc_iso(NOW - 10), last_searched_at=NOW - 3600,
                     request_created_at=NOW - 7200)
    d = compute_cadence(item, _cfg(), NOW)
    assert d["due"] is True and d["status"] == "aired"


# ── movie availability gate ──────────────────────────────────────────────────


def test_movie_gated_before_release_plus_offset():
    item = _movie_item(release_date_utc=_utc_iso(NOW), last_searched_at=NOW - 10,
                        request_created_at=NOW - 100)
    d = compute_cadence(item, _cfg(movie_release_offset_days=2.0), NOW)
    assert d["due"] is False and d["status"] == "gated"
    assert d["next_due"] == NOW + 2 * 86400


def test_movie_due_once_release_plus_offset_has_passed():
    item = _movie_item(release_date_utc=_utc_iso(NOW - 3 * 86400),
                        last_searched_at=NOW - 10, request_created_at=NOW - 100)
    d = compute_cadence(item, _cfg(movie_release_offset_days=2.0), NOW)
    assert d["due"] is True


# ── backlog draining overrides everything ────────────────────────────────────


def test_draining_backlog_is_due_even_while_gated():
    item = _tv_item(next_air_utc=_utc_iso(NOW + 999999), search_backlog=3,
                     last_searched_at=NOW - 10, request_created_at=NOW - 100)
    d = compute_cadence(item, _cfg(), NOW)
    assert d["due"] is True and d["status"] == "draining"
    assert "3 episode" in d["detail"]


# ── content-age back-off ─────────────────────────────────────────────────────


def _tiers():
    return [
        BackoffTier(older_than_days=7, search_every_seconds=86400),
        BackoffTier(older_than_days=90, search_every_seconds=7 * 86400),
    ]


def test_backoff_not_due_within_the_tier_gap():
    old_air = NOW - 30 * 86400  # 30 days old -> the 7-day tier (1-day gap) applies
    item = _tv_item(air_anchor_utc=_utc_iso(old_air), last_searched_at=NOW - 3600,
                     request_created_at=NOW - 40 * 86400)
    d = compute_cadence(item, _cfg(backoff=_tiers()), NOW)
    assert d["due"] is False and d["status"] == "backoff"


def test_backoff_due_once_the_tier_gap_has_elapsed():
    old_air = NOW - 30 * 86400
    item = _tv_item(air_anchor_utc=_utc_iso(old_air), last_searched_at=NOW - 2 * 86400,
                     request_created_at=NOW - 40 * 86400)
    d = compute_cadence(item, _cfg(backoff=_tiers()), NOW)
    assert d["due"] is True and d["status"] == "due"


def test_older_content_gets_the_wider_tier_gap():
    very_old_air = NOW - 200 * 86400  # -> the 90-day tier (7-day gap)
    item = _tv_item(air_anchor_utc=_utc_iso(very_old_air), last_searched_at=NOW - 2 * 86400,
                     request_created_at=NOW - 300 * 86400)
    d = compute_cadence(item, _cfg(backoff=_tiers()), NOW)
    # Only 2 days since last search but the wide tier needs 7 -> still backed off.
    assert d["due"] is False
    assert d["next_due"] == item["last_searched_at"] + 7 * 86400


def test_no_backoff_tiers_configured_means_always_due():
    old_air = NOW - 300 * 86400
    item = _tv_item(air_anchor_utc=_utc_iso(old_air), last_searched_at=NOW - 10,
                     request_created_at=NOW - 400 * 86400)
    d = compute_cadence(item, _cfg(backoff=[]), NOW)
    assert d["due"] is True and d["status"] == "due" and d["detail"] == "no back-off"


# ── fresh-episode override caps (never slows) the back-off ─────────────────


def test_fresh_episode_caps_a_wider_backoff_gap():
    # Aired 5h ago (within fresh_episode_hours) and last searched 3h ago — AFTER
    # the air date, so this doesn't trip the earlier "just aired" branch — but a
    # 0-day-threshold tier (always applicable) would otherwise back it off 7
    # days. Fresh must cap that gap to fresh_episode_every_seconds (2h here).
    air = NOW - 5 * 3600
    item = _tv_item(air_anchor_utc=_utc_iso(air), last_searched_at=NOW - 3 * 3600,
                     request_created_at=NOW - 400 * 86400)
    cfg = _cfg(backoff=[BackoffTier(older_than_days=0, search_every_seconds=7 * 86400)],
               fresh_episode_hours=48.0, fresh_episode_every_seconds=7200)
    d = compute_cadence(item, cfg, NOW)
    # 3h since last search >= the capped 2h fresh gap -> due now, not backed off 7 days.
    assert d["due"] is True
    assert "fresh" in d["detail"]


def test_fresh_episode_override_never_widens_an_already_tighter_gap():
    recent_air = NOW - 3600
    item = _tv_item(air_anchor_utc=_utc_iso(recent_air), last_searched_at=NOW - 1800,
                     request_created_at=NOW - 400 * 86400)
    # Backoff tier gap (1h) is already tighter than the fresh cap (2h) — fresh
    # must not widen it back out to 2h.
    cfg = _cfg(backoff=[BackoffTier(older_than_days=0, search_every_seconds=3600)],
               fresh_episode_hours=48.0, fresh_episode_every_seconds=7200)
    d = compute_cadence(item, cfg, NOW)
    # 30 min since last search < 1h gap -> still backed off.
    assert d["due"] is False
    assert d["next_due"] == item["last_searched_at"] + 3600
