"""Tests for AirDCPP.search_release_candidates — directory-results-first
hub search with a fallback to unrestricted (any file type) when the
directory-scoped attempt comes back completely empty. _retry_on_401 is
monkeypatched to simulate the AirDC++ webapi rather than hitting a real
hub, same convention as test_search_rate_limit.py.
"""
import asyncio

import httpx

from dcbridge.airdcpp import AirDCPP
from dcbridge.config import AirDCPPCfg


def _client() -> AirDCPP:
    return AirDCPP(AirDCPPCfg(url="http://fake", username="u", password="p", min_search_interval_seconds=0))


def _resp(status_code=200, body=None):
    return httpx.Response(
        status_code, json=body if body is not None else {},
        request=httpx.Request("GET", "http://fake"),
    )


class Recorder:
    """Routes create/hub_search/results/delete calls by URL shape, tracking
    every dispatched query's file_type and the sequence of instance ids
    used, without needing a real AirDC++ server."""

    def __init__(self, results_by_file_type: dict):
        self.results_by_file_type = results_by_file_type  # {"directory": [...], None: [...]}
        self.dispatched: list[tuple[str, str | None]] = []  # (pattern, file_type)
        self.dispatched_extensions: list[list[str] | None] = []
        self.deleted_instances: list[int] = []
        self._next_id = 0
        self._file_type_by_instance: dict[int, str | None] = {}

    async def __call__(self, method, path, **kw):
        if method == "POST" and path == "/api/v1/search":
            self._next_id += 1
            return _resp(200, {"id": self._next_id})
        if method == "POST" and path.endswith("/hub_search"):
            iid = int(path.split("/")[4])
            query = kw["json"]["query"]
            file_type = query.get("file_type")
            self._file_type_by_instance[iid] = file_type
            self.dispatched.append((query["pattern"], file_type))
            self.dispatched_extensions.append(query.get("extensions"))
            return _resp(200, {})
        if method == "GET" and "/results/" in path:
            iid = int(path.split("/")[4])
            ft = self._file_type_by_instance.get(iid)
            return _resp(200, self.results_by_file_type.get(ft, []))
        if method == "DELETE" and path.startswith("/api/v1/search/"):
            self.deleted_instances.append(int(path.rsplit("/", 1)[1]))
            return _resp(200, {})
        raise AssertionError(f"unexpected {method} {path}")


def test_searches_directory_first_and_returns_those_results_when_non_empty():
    ad = _client()
    rec = Recorder({"directory": [{"name": "Some.Movie.2020-GROUP"}], None: [{"name": "should not be used"}]})
    ad._retry_on_401 = rec

    iid, results = asyncio.run(ad.search_release_candidates("Some Movie 2020", wait=0))

    assert results == [{"name": "Some.Movie.2020-GROUP"}]
    assert rec.dispatched == [("Some Movie 2020", "directory")]  # no fallback needed
    assert iid is not None
    assert rec.deleted_instances == []  # success path — caller owns deleting it


def test_falls_back_to_unrestricted_search_when_directory_is_empty():
    ad = _client()
    rec = Recorder({"directory": [], None: [{"name": "Loose.File.Only.Release.2020.mkv"}]})
    ad._retry_on_401 = rec

    iid, results = asyncio.run(ad.search_release_candidates("Some Show S01E01", wait=0))

    assert results == [{"name": "Loose.File.Only.Release.2020.mkv"}]
    assert rec.dispatched == [("Some Show S01E01", "directory"), ("Some Show S01E01", None)]
    assert iid is not None
    assert rec.deleted_instances == [1]  # the empty directory-only instance was cleaned up


def test_both_attempts_empty_returns_no_results_but_a_live_instance():
    ad = _client()
    rec = Recorder({"directory": [], None: []})
    ad._retry_on_401 = rec

    iid, results = asyncio.run(ad.search_release_candidates("Nothing Ever Found", wait=0))

    assert results == []
    assert iid is not None  # still a real, caller-owned instance — not a failure
    assert rec.dispatched == [("Nothing Ever Found", "directory"), ("Nothing Ever Found", None)]


def test_extensions_are_passed_through_on_both_attempts():
    ad = _client()
    rec = Recorder({"directory": [], None: [{"name": "x"}]})
    ad._retry_on_401 = rec

    asyncio.run(ad.search_release_candidates("Query", wait=0, extensions=["mkv", "avi"]))

    assert rec.dispatched_extensions == [["mkv", "avi"], ["mkv", "avi"]]


def test_dispatch_failure_on_the_directory_attempt_still_tries_the_fallback():
    # The directory-scoped dispatch itself fails outright (e.g. a transient
    # hub error) — that instance is discarded and a fresh one is used for
    # the unrestricted retry, same as an empty-results outcome would.
    ad = _client()
    calls = {"n": 0}

    async def flaky(method, path, **kw):
        if method == "POST" and path == "/api/v1/search":
            calls["n"] += 1
            return _resp(200, {"id": calls["n"]})
        if method == "POST" and path.endswith("/hub_search"):
            body = kw["json"]["query"]
            if body.get("file_type") == "directory":
                return _resp(500, {})  # the directory-scoped dispatch fails
            return _resp(200, {})
        if method == "GET" and "/results/" in path:
            return _resp(200, [{"name": "found-on-retry"}])
        if method == "DELETE":
            return _resp(200, {})
        raise AssertionError(f"unexpected {method} {path}")

    ad._retry_on_401 = flaky
    iid, results = asyncio.run(ad.search_release_candidates("Query", wait=0))
    assert iid == 2  # the second (fallback) instance, not the discarded first
    assert results == [{"name": "found-on-retry"}]


def test_instance_creation_failure_on_the_first_attempt_is_a_clean_failure():
    # A lower-level connectivity issue (can't even create a search instance)
    # aborts outright rather than trying a second time — matches the
    # pre-existing behavior every call site already handled (iid is None ->
    # skip this attempt, the caller's own title-variant loop moves on).
    ad = _client()

    async def cant_create(method, path, **kw):
        if method == "POST" and path == "/api/v1/search":
            return _resp(500, {})
        raise AssertionError(f"unexpected {method} {path}")

    ad._retry_on_401 = cant_create
    iid, results = asyncio.run(ad.search_release_candidates("Query", wait=0))
    assert iid is None
    assert results == []


def test_dispatch_failure_on_both_attempts_returns_none():
    ad = _client()

    async def always_fail_dispatch(method, path, **kw):
        if method == "POST" and path == "/api/v1/search":
            return _resp(200, {"id": 1})
        if method == "POST" and path.endswith("/hub_search"):
            return _resp(500, {})
        if method == "DELETE":
            return _resp(200, {})
        raise AssertionError(f"unexpected {method} {path}")

    ad._retry_on_401 = always_fail_dispatch
    iid, results = asyncio.run(ad.search_release_candidates("Query", wait=0))
    assert iid is None
    assert results == []
