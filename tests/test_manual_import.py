"""Tests for arr.sonarr_manual_import_releases — importing a completed grab
whose video file name Sonarr can't parse (seen live 2026-10-18: AFO's
"afo-myadventureswithsuperman-0305-720-web.mkv" sat unimported for two days
while the bridge re-fired RescanSeries every sweep), using the release
folder to tell Sonarr which episode it is.
"""
import asyncio
from contextlib import asynccontextmanager

import dcbridge.arr as arr
from dcbridge.config import ArrCfg, Config

REL = "My.Adventures.With.Superman.S03E05.720p.WEB.H264-AFO"
FOLDER = f"/share/TV.Series/Superman/Season.3/{REL}"
QUALITY = {"quality": {"id": 5, "name": "WEBDL-720p"}, "revision": {"version": 1}}


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body

    def json(self):
        return self._json


class FakeSonarr:
    def __init__(self, manual_rows):
        self.manual_rows = manual_rows
        self.commands: list[dict] = []
        self.manual_params: list[dict] = []

    async def get(self, url, params=None, headers=None):
        if url.endswith("/api/v3/series/858"):
            return FakeResponse(200, {"path": "/share/TV.Series/Superman"})
        if url.endswith("/api/v3/episode"):
            return FakeResponse(200, [
                {"id": 105, "seasonNumber": 3, "episodeNumber": 5},
                {"id": 106, "seasonNumber": 3, "episodeNumber": 6},
            ])
        if url.endswith("/api/v3/manualimport"):
            self.manual_params.append(params)
            return FakeResponse(200, self.manual_rows)
        if url.endswith("/api/v3/parse"):
            assert params["title"] == REL
            return FakeResponse(200, {"parsedEpisodeInfo": {
                "quality": QUALITY, "languages": [{"id": 1, "name": "English"}],
                "releaseGroup": "AFO"}})
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url, headers=None, json=None):
        self.commands.append(json)
        return FakeResponse(201, {"id": 1})


def _run(monkeypatch, rows, releases=(("S03E05", REL),)):
    sonarr = FakeSonarr(rows)

    @asynccontextmanager
    async def _session():
        yield sonarr

    monkeypatch.setattr(arr, "http_session", _session)
    cfg = Config.model_construct(sonarr=ArrCfg(url="http://sonarr:8989", api_key="sk"))
    n = asyncio.run(arr.sonarr_manual_import_releases(cfg, "sonarr:858", list(releases)))
    return n, sonarr


def _row(name, episodes=(), rejections=()):
    return {"path": f"{FOLDER}/{name}", "episodes": list(episodes),
            "rejections": list(rejections), "languages": []}


def test_unparseable_file_is_imported_as_the_folders_episode(monkeypatch):
    n, sonarr = _run(monkeypatch, [
        _row("afo-myadventureswithsuperman-0305-720-web.mkv"),
        _row("Sample/afo-myadventureswithsuperman-0305-720-web-sample.mkv"),
    ])
    assert n == 1
    assert sonarr.manual_params[0]["folder"] == FOLDER
    (cmd,) = sonarr.commands
    assert cmd["name"] == "ManualImport"
    (f,) = cmd["files"]
    assert f["path"].endswith("0305-720-web.mkv")
    assert f["episodeIds"] == [105] and f["seriesId"] == 858
    assert f["quality"] == QUALITY and f["releaseGroup"] == "AFO"


def test_rows_outside_the_release_folder_are_ignored(monkeypatch):
    # Live Sonarr returns every file in the series for a folder inside it.
    other = dict(_row("x.mkv"), path="/share/TV.Series/Superman/Season.3/Other.S03E06-X/x.mkv")
    n, sonarr = _run(monkeypatch, [other, _row("afo-0305.mkv")])
    assert n == 1
    assert [f["path"] for f in sonarr.commands[0]["files"]] == [f"{FOLDER}/afo-0305.mkv"]


def test_file_sonarr_matched_to_another_episode_is_left_alone(monkeypatch):
    n, sonarr = _run(monkeypatch, [_row("x.mkv", episodes=[{"id": 106}])])
    assert n == 0 and sonarr.commands == []


def test_rejected_file_is_left_alone(monkeypatch):
    n, sonarr = _run(monkeypatch, [_row("x.mkv", rejections=[{"reason": "Not an upgrade"}])])
    assert n == 0 and sonarr.commands == []


def test_ambiguous_folder_with_two_videos_is_left_alone(monkeypatch):
    n, sonarr = _run(monkeypatch, [_row("a.mkv"), _row("b.mkv")])
    assert n == 0 and sonarr.commands == []


def test_unknown_episode_key_is_skipped(monkeypatch):
    n, sonarr = _run(monkeypatch, [_row("x.mkv")], releases=(("S09E01", REL),))
    assert n == 0 and sonarr.manual_params == [] and sonarr.commands == []
