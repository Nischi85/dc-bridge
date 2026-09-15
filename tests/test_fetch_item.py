"""Tests for arr.fetch_movie_item / fetch_series_item — the "check other
releases" feature's item builder. Unlike resync_one_movie/resync_one_series
(which refuse anything with hasFile=true, since they exist to feed the
AUTOMATIC poller's worklist), these must work for an item you already
have — that's the whole point of browsing alternatives to it. Built
straight from Radarr/Sonarr each call, never touching tracked_items.
"""
import asyncio
from contextlib import asynccontextmanager

import dcbridge.arr as arr
from dcbridge.config import ArrCfg, Config, QualityCfg


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}

    def json(self):
        return self._json


class FakeHttp:
    def __init__(self, routes: dict):
        # routes: {url_substring: json_body_or_FakeResponse}
        self._routes = routes
        self.get_urls: list[str] = []

    async def get(self, url, params=None, headers=None):
        self.get_urls.append(url)
        for sub, body in self._routes.items():
            if sub in url:
                return body if isinstance(body, FakeResponse) else FakeResponse(200, body)
        raise AssertionError(f"unexpected GET {url}")


@asynccontextmanager
async def _session(http):
    yield http


def _patch_session(monkeypatch, http):
    monkeypatch.setattr(arr, "http_session", lambda: _session(http))


def _cfg():
    return Config.model_construct(
        radarr=ArrCfg(url="http://radarr:7878", api_key="rk"),
        sonarr=ArrCfg(url="http://sonarr:8989", api_key="sk"),
        quality=QualityCfg(episode_size_mb=(100, 5000), movie_size_mb=(500, 20000)),
        path_translate=[],
    )


# ── fetch_movie_item ────────────────────────────────────────────────────

def test_fetch_movie_item_works_even_when_hasfile_is_true(monkeypatch):
    # The core difference from resync_one_movie: an already-downloaded
    # movie must still be fetchable, since that's the normal case for
    # "check other releases".
    http = FakeHttp({
        "/api/v3/movie/42": {"id": 42, "title": "Some Movie", "year": 2020,
                             "hasFile": True, "monitored": True,
                             "rootFolderPath": "/share/Movies", "qualityProfileId": 3},
        "/api/v3/qualityprofile": [],
    })
    _patch_session(monkeypatch, http)
    item = asyncio.run(arr.fetch_movie_item(_cfg(), "42"))
    assert item == {
        "id": "radarr:42", "kind": "movie", "title": "Some Movie", "year": 2020,
        "target_dir_fs": "/share/Movies", "quality_priority": [],
    }


def test_fetch_movie_item_works_when_unmonitored(monkeypatch):
    http = FakeHttp({
        "/api/v3/movie/42": {"id": 42, "title": "Unmonitored Movie", "year": 2019,
                             "hasFile": True, "monitored": False,
                             "rootFolderPath": "/share/Movies", "qualityProfileId": 3},
        "/api/v3/qualityprofile": [],
    })
    _patch_session(monkeypatch, http)
    item = asyncio.run(arr.fetch_movie_item(_cfg(), "42"))
    assert item is not None and item["title"] == "Unmonitored Movie"


def test_fetch_movie_item_falls_back_to_folder_paths_parent(monkeypatch):
    http = FakeHttp({
        "/api/v3/movie/42": {"id": 42, "title": "X", "year": 2020, "rootFolderPath": "",
                             "path": "/share/Movies/X.2020", "qualityProfileId": 3},
        "/api/v3/qualityprofile": [],
    })
    _patch_session(monkeypatch, http)
    item = asyncio.run(arr.fetch_movie_item(_cfg(), "42"))
    assert item["target_dir_fs"] == "/share/Movies"


def test_fetch_movie_item_none_when_movie_missing(monkeypatch):
    http = FakeHttp({"/api/v3/movie/999": FakeResponse(404)})
    _patch_session(monkeypatch, http)
    assert asyncio.run(arr.fetch_movie_item(_cfg(), "999")) is None


def test_fetch_movie_item_none_without_any_resolvable_path(monkeypatch):
    http = FakeHttp({
        "/api/v3/movie/42": {"id": 42, "title": "X", "rootFolderPath": "", "path": ""},
        "/api/v3/qualityprofile": [],
    })
    _patch_session(monkeypatch, http)
    assert asyncio.run(arr.fetch_movie_item(_cfg(), "42")) is None


def test_fetch_movie_item_none_without_radarr_api_key():
    cfg = Config.model_construct(radarr=ArrCfg(url="http://radarr:7878", api_key=""))
    assert asyncio.run(arr.fetch_movie_item(cfg, "42")) is None


def test_fetch_movie_item_none_for_a_non_numeric_id():
    assert asyncio.run(arr.fetch_movie_item(_cfg(), "abc")) is None


# ── fetch_series_item ─────────────────────────────────────────────────────

def test_fetch_series_item_works_even_when_fully_downloaded(monkeypatch):
    http = FakeHttp({
        "/api/v3/series/9": {"id": 9, "title": "Some Show", "year": 2020,
                             "monitored": True, "path": "/share/TV.Series/Some.Show",
                             "qualityProfileId": 5},
        "/api/v3/episode": [
            {"seasonNumber": 1, "episodeNumber": 1, "airDateUtc": "2020-01-02T00:00:00Z", "hasFile": True},
            {"seasonNumber": 1, "episodeNumber": 2, "airDateUtc": "2020-01-09T00:00:00Z", "hasFile": True},
        ],
        "/api/v3/qualityprofile": [],
    })
    _patch_session(monkeypatch, http)
    item = asyncio.run(arr.fetch_series_item(_cfg(), "9"))
    assert item == {
        "id": "sonarr:9", "kind": "tv", "title": "Some Show", "year": 2020,
        "target_dir_fs": "/share/TV.Series/Some.Show",
        "episode_air_years": {"S01E01": 2020, "S01E02": 2020},
        "quality_priority": [],
    }


def test_fetch_series_item_none_without_a_path(monkeypatch):
    http = FakeHttp({"/api/v3/series/9": {"id": 9, "title": "X", "path": ""}})
    _patch_session(monkeypatch, http)
    assert asyncio.run(arr.fetch_series_item(_cfg(), "9")) is None


def test_fetch_series_item_survives_episode_fetch_failure(monkeypatch):
    # Episode fetch failing shouldn't sink the whole item — just leaves
    # episode_air_years empty (tv_year_guard falls back to the show year).
    http = FakeHttp({
        "/api/v3/series/9": {"id": 9, "title": "X", "year": 2020, "path": "/share/TV.Series/X"},
        "/api/v3/episode": FakeResponse(500),
        "/api/v3/qualityprofile": [],
    })
    _patch_session(monkeypatch, http)
    item = asyncio.run(arr.fetch_series_item(_cfg(), "9"))
    assert item is not None and item["episode_air_years"] == {}


def test_fetch_series_item_none_for_a_non_numeric_id():
    assert asyncio.run(arr.fetch_series_item(_cfg(), "abc")) is None
