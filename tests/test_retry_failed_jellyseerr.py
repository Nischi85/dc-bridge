"""Tests for retry_failed_jellyseerr_requests — auto-retries a Jellyseerr
request Jellyseerr marked FAILED only when the underlying movie/series
already exists in Radarr/Sonarr, the exact signature of a collection-request
race (Radarr's own add endpoint 409s when two near-simultaneous adds for
different titles collide on its database; the media still gets added by
whichever request won). A genuine failure (nothing in *arr) is left alone.
"""
import asyncio

from dcbridge.arr import retry_failed_jellyseerr_requests
from dcbridge.config import ArrCfg, Config, JellyseerrCfg


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}

    def json(self):
        return self._json


class FakeHttp:
    """Routes GET/POST calls by URL substring to canned responses, and
    records every retry POST so tests can assert exactly which requests
    got retried."""

    def __init__(self, requests_page, arr_existence: dict, retry_status: int = 200):
        self._requests_page = requests_page
        self._arr_existence = arr_existence  # {(mtype, id): bool}
        self._retry_status = retry_status
        self.retried_ids: list[int] = []

    async def get(self, url, params=None, headers=None):
        if "/api/v1/request" in url:
            return FakeResponse(200, self._requests_page)
        if "/api/v3/movie" in url:
            exists = self._arr_existence.get(("movie", params.get("tmdbId")), False)
            return FakeResponse(200, [{"id": 1}] if exists else [])
        if "/api/v3/series" in url:
            exists = self._arr_existence.get(("tv", params.get("tvdbId")), False)
            return FakeResponse(200, [{"id": 1}] if exists else [])
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url, headers=None):
        assert "/retry" in url
        req_id = int(url.rsplit("/", 2)[-2])
        self.retried_ids.append(req_id)
        return FakeResponse(self._retry_status)


def _cfg():
    return Config.model_construct(
        jellyseerr=JellyseerrCfg(url="http://jellyseerr:5055", api_key="jk"),
        radarr=ArrCfg(url="http://radarr:7878", api_key="rk"),
        sonarr=ArrCfg(url="http://sonarr:8989", api_key="sk"),
    )


def _page(requests, total=None):
    return {"results": requests, "pageInfo": {"results": total if total is not None else len(requests)}}


def _movie_req(req_id, tmdb_id):
    return {"id": req_id, "media": {"mediaType": "movie", "tmdbId": tmdb_id}}


def _tv_req(req_id, tvdb_id):
    return {"id": req_id, "media": {"mediaType": "tv", "tvdbId": tvdb_id}}


def test_retries_only_the_request_whose_movie_already_exists_in_radarr():
    reqs = [_movie_req(219, 12437), _movie_req(221, 52520)]
    http = FakeHttp(_page(reqs), {("movie", 12437): True, ("movie", 52520): False})
    result = asyncio.run(retry_failed_jellyseerr_requests(_cfg(), http))
    assert http.retried_ids == [219]
    assert result == {"retried": 1, "left_failed": 1}


def test_tv_request_checked_against_sonarr_by_tvdbid():
    reqs = [_tv_req(50, 153021)]
    http = FakeHttp(_page(reqs), {("tv", 153021): True})
    result = asyncio.run(retry_failed_jellyseerr_requests(_cfg(), http))
    assert http.retried_ids == [50]
    assert result["retried"] == 1


def test_no_failed_requests_is_a_noop():
    http = FakeHttp(_page([]), {})
    result = asyncio.run(retry_failed_jellyseerr_requests(_cfg(), http))
    assert result == {"retried": 0, "left_failed": 0}
    assert http.retried_ids == []


def test_genuine_failure_with_nothing_in_arr_is_left_alone():
    reqs = [_movie_req(300, 999999)]
    http = FakeHttp(_page(reqs), {})  # nothing exists anywhere
    result = asyncio.run(retry_failed_jellyseerr_requests(_cfg(), http))
    assert http.retried_ids == []
    assert result == {"retried": 0, "left_failed": 1}


def test_disabled_without_jellyseerr_config():
    cfg = Config.model_construct(jellyseerr=JellyseerrCfg())  # no url/api_key
    result = asyncio.run(retry_failed_jellyseerr_requests(cfg, FakeHttp(_page([]), {})))
    assert result == {"skipped": "disabled"}


def test_retry_endpoint_failure_counts_as_left_failed_not_retried():
    reqs = [_movie_req(219, 12437)]
    http = FakeHttp(_page(reqs), {("movie", 12437): True}, retry_status=500)
    result = asyncio.run(retry_failed_jellyseerr_requests(_cfg(), http))
    assert http.retried_ids == [219]  # attempted
    assert result == {"retried": 0, "left_failed": 1}  # but didn't count as success
