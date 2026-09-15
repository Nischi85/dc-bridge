"""Tests for poller.list_release_candidates / poller.select_release — the
human-triggered "check other releases" pair behind web.py's
GET /candidates/{item} and POST /candidates/{item}/select (media-audit's
"Check other releases" button). AirDC++ and state are fakes; no real
network or sqlite involved.
"""
import asyncio

import dcbridge.poller as poller
from dcbridge.config import (
    AirDCPPCfg, ArrCfg, Config, LanguageRule, PathMap, QualityCfg,
)


def _cfg(*, priority=None, language_priority=None) -> Config:
    return Config(
        airdcpp=AirDCPPCfg(url="http://x", username="u", password="p"),
        sonarr=ArrCfg(url="http://sonarr"),
        radarr=ArrCfg(url="http://radarr"),
        path_map=PathMap(linux_root="/mnt/zzd/share/fin", windows_root="Z:\\"),
        quality=QualityCfg(
            episode_size_mb=(100, 20000), movie_size_mb=(500, 20000),
            # A non-empty priority is required for passes_quality to accept
            # anything at all (an empty priority falls back to the legacy
            # accepted_keywords/resolutions pair, both empty by default) —
            # match any 720p/1080p release, which is all these tests use.
            priority=priority if priority is not None else ["1080p", "720p"],
            language_priority=language_priority or [],
        ),
    )


def _movie_item(target_dir_fs="/mnt/zzd/share/fin/Movies"):
    return {"id": "radarr:1", "kind": "movie", "title": "Some Movie", "year": 2020,
            "target_dir_fs": target_dir_fs, "quality_priority": []}


def _tv_item(target_dir_fs="/mnt/zzd/share/fin/TV.Series"):
    return {"id": "sonarr:9", "kind": "tv", "title": "Some Show", "year": 2020,
            "target_dir_fs": target_dir_fs, "quality_priority": []}


def _dir_result(name, path, size_mb, rid):
    return {"type": {"id": "directory", "str": "Directory"}, "name": name,
            "path": path, "id": rid, "size": size_mb * 1024 * 1024, "users": {"count": 3}}


class FakeAd:
    def __init__(self, results, bundles=None):
        self._results = results
        self._bundles = bundles or []
        self.removed_bundle_ids = []
        self.queued = []
        self._next_iid = 0
        self.instances_deleted = []
        self.hub_searches = []

    async def ensure_auth(self):
        pass

    async def create_search_instance(self):
        self._next_iid += 1
        return self._next_iid

    async def hub_search(self, iid, query, extensions=None):
        self.hub_searches.append(query)
        return True

    async def get_results(self, iid, start, count):
        return self._results

    async def delete_instance(self, iid):
        self.instances_deleted.append(iid)

    async def list_bundles(self, start=0, count=200):
        return self._bundles

    async def remove_bundle(self, bundle_id, remove_finished=False):
        self.removed_bundle_ids.append((bundle_id, remove_finished))
        return True

    async def queue_result(self, iid, tth_or_dir_id, target_dir):
        self.queued.append((tth_or_dir_id, target_dir))
        return {"bundle_info": {"id": 999}}


class FakeState:
    def __init__(self):
        self.cleared = []
        self.completed: dict[tuple, bool] = {}
        self.marked = []

    async def clear_completed(self, item_id, key):
        self.cleared.append((item_id, key))
        self.completed.pop((item_id, key), None)

    async def is_completed(self, item_id, key):
        return self.completed.get((item_id, key), False)

    async def mark_completed(self, item_id, key, bundle_id, release_name):
        self.marked.append((item_id, key, bundle_id, release_name))
        self.completed[(item_id, key)] = True


# ── list_release_candidates ─────────────────────────────────────────────

def test_lists_every_passing_release_scored_and_sorted(monkeypatch):
    results = [
        _dir_result("Some.Movie.2020.1080p.BluRay.x264-GROUP", "/Movies/Some.Movie.2020.1080p.BluRay.x264-GROUP/", 8000, "d1"),
        _dir_result("Some.Movie.2020.NORDIC.1080p.BluRay.x264-GROUP2", "/Movies/Some.Movie.2020.NORDIC.1080p.BluRay.x264-GROUP2/", 8000, "d2"),
    ]
    ad = FakeAd(results)
    cfg = _cfg(language_priority=[LanguageRule(path_contains="Movies", languages=["swedish", "english"])])
    out = asyncio.run(poller.list_release_candidates(cfg, ad, _movie_item(), "movie", wait=0))

    assert len(out) == 2
    names = [c["release_name"] for c in out]
    assert "Some.Movie.2020.NORDIC.1080p.BluRay.x264-GROUP2" in names
    # Nordic wins first — language_priority applies under /Movies here.
    assert out[0]["release_name"].endswith("GROUP2")
    assert out[0]["languages"] == ["swedish"]
    assert out[1]["languages"] == ["english"]
    assert all(c["whole_folder"] for c in out)
    assert ad.instances_deleted  # the listing instance was cleaned up


def test_rejects_a_release_that_fails_the_normal_guards(monkeypatch):
    # Wrong year — same guard _select_candidates always applies.
    results = [_dir_result("Some.Movie.2011.1080p.BluRay.x264-GROUP", "/Movies/Some.Movie.2011.1080p.BluRay.x264-GROUP/", 8000, "d1")]
    ad = FakeAd(results)
    out = asyncio.run(poller.list_release_candidates(_cfg(), ad, _movie_item(), "movie", wait=0))
    assert out == []


def test_tv_uses_the_episode_key_in_the_search_query(monkeypatch):
    results = [_dir_result(
        "Some.Show.S01E02.1080p.WEB.x264-GROUP",
        "/TV/Some.Show.S01E02.1080p.WEB.x264-GROUP/", 2000, "d1",
    )]
    ad = FakeAd(results)
    out = asyncio.run(poller.list_release_candidates(_cfg(), ad, _tv_item(), "S01E02", wait=0))
    assert len(out) == 1
    assert "S01E02" in ad.hub_searches[0]


def test_empty_hub_search_returns_no_candidates(monkeypatch):
    ad = FakeAd([])

    async def fake_hub_search(iid, query, extensions=None):
        return False

    ad.hub_search = fake_hub_search
    out = asyncio.run(poller.list_release_candidates(_cfg(), ad, _movie_item(), "movie", wait=0))
    assert out == []


# ── select_release ────────────────────────────────────────────────────────

def test_select_clears_marker_removes_stale_bundle_and_queues_the_chosen_one(monkeypatch):
    old_bundle = {"id": 42, "name": "Some.Movie.2020.OLDGROUP.720p.WEB.x264-BAD"}
    results = [
        _dir_result("Some.Movie.2020.1080p.BluRay.x264-GOOD", "/Movies/Some.Movie.2020.1080p.BluRay.x264-GOOD/", 8000, "d1"),
        _dir_result("Some.Movie.2020.720p.WEB.x264-OTHER", "/Movies/Some.Movie.2020.720p.WEB.x264-OTHER/", 3000, "d2"),
    ]
    ad = FakeAd(results, bundles=[old_bundle])
    state = FakeState()
    state.completed[("radarr:1", "movie")] = True  # a stale marker from the old grab

    queued = asyncio.run(poller.select_release(
        _cfg(), state, ad, _movie_item(), "movie", "Some.Movie.2020.1080p.BluRay.x264-GOOD",
    ))

    assert queued == 1
    assert state.cleared == [("radarr:1", "movie")]
    assert ("radarr:1", "movie") not in state.completed or state.marked  # cleared, then re-marked by the queue
    assert ad.removed_bundle_ids == [(42, False)]
    assert ad.queued == [("d1", "Z:\\Movies\\")]
    assert state.marked[0][3] == "Some.Movie.2020.1080p.BluRay.x264-GOOD"


def test_select_leaves_an_unrelated_bundle_alone(monkeypatch):
    other_bundle = {"id": 7, "name": "A.Completely.Different.Movie.2020.1080p-GROUP"}
    results = [_dir_result("Some.Movie.2020.1080p.BluRay.x264-GOOD", "/Movies/Some.Movie.2020.1080p.BluRay.x264-GOOD/", 8000, "d1")]
    ad = FakeAd(results, bundles=[other_bundle])
    state = FakeState()
    asyncio.run(poller.select_release(_cfg(), state, ad, _movie_item(), "movie", "Some.Movie.2020.1080p.BluRay.x264-GOOD"))
    assert ad.removed_bundle_ids == []


def test_select_returns_none_when_the_chosen_release_is_gone_on_a_fresh_search(monkeypatch):
    results = [_dir_result("Some.Movie.2020.720p.WEB.x264-OTHER", "/Movies/Some.Movie.2020.720p.WEB.x264-OTHER/", 3000, "d2")]
    ad = FakeAd(results)
    state = FakeState()
    queued = asyncio.run(poller.select_release(
        _cfg(), state, ad, _movie_item(), "movie", "Some.Movie.2020.1080p.BluRay.x264-GOOD",
    ))
    assert queued is None
    assert ad.queued == []
    # Marker was still cleared up front (the caller already deleted the old
    # file — leaving the marker in place after that would be worse).
    assert state.cleared == [("radarr:1", "movie")]


def test_select_tv_matches_bundles_by_episode_key(monkeypatch):
    old_bundle = {"id": 5, "name": "Some.Show.S01E02.720p.WEB.x264-OLD"}
    unrelated_episode_bundle = {"id": 6, "name": "Some.Show.S01E03.720p.WEB.x264-KEEP"}
    results = [_dir_result(
        "Some.Show.S01E02.1080p.WEB.x264-NEW",
        "/TV/Some.Show.S01E02.1080p.WEB.x264-NEW/", 2000, "d1",
    )]
    ad = FakeAd(results, bundles=[old_bundle, unrelated_episode_bundle])
    state = FakeState()
    queued = asyncio.run(poller.select_release(
        _cfg(), state, ad, _tv_item(), "S01E02", "Some.Show.S01E02.1080p.WEB.x264-NEW",
    ))
    assert queued == 1
    assert ad.removed_bundle_ids == [(5, False)]
    assert state.cleared == [("sonarr:9", "S01E02")]
