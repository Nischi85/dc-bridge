from __future__ import annotations
import asyncio

from dcbridge.config import Config, PathMap
from dcbridge.repair import repair_release


class FakeAirDCPP:
    def __init__(self, results, queue_ok=True, create_instance_ok=True, hub_search_ok=True):
        self._results = results
        self._queue_ok = queue_ok
        self._create_instance_ok = create_instance_ok
        self._hub_search_ok = hub_search_ok
        self.queued: list[tuple[str, str]] = []
        self.deleted_instances: list[int] = []

    async def create_search_instance(self):
        return 1 if self._create_instance_ok else None

    async def hub_search(self, iid, query, extensions=None):
        return self._hub_search_ok

    async def get_results(self, iid, start, count):
        return self._results

    async def queue_result(self, iid, result_id, target_dir):
        if not self._queue_ok:
            return None
        self.queued.append((result_id, target_dir))
        return {"ok": True}

    async def delete_instance(self, iid):
        self.deleted_instances.append(iid)


def _cfg() -> Config:
    return Config.model_construct(path_map=PathMap(linux_root="/mnt/zzd/share", windows_root="Z:\\"))


def _dir_result(path: str, result_id: str, size: int = 1000, dupe: dict | None = None) -> dict:
    return {
        "path": path,
        "id": result_id,
        "size": size,
        "type": {"id": "directory"},
        "dupe": dupe,
    }


def _repair(ad, release_dir, missing_files):
    return asyncio.run(repair_release(_cfg(), ad, release_dir, missing_files, settle_seconds=0))


def test_exact_name_directory_match_is_queued_as_a_whole_folder():
    release_dir = "/mnt/zzd/share/Movies/Some.Release-GROUP"
    # A "share_full" dupe flag (AirDC++ believes it already has everything)
    # must NOT block the whole-folder queue — that belief is exactly what
    # made the old per-file approach fail on files that were genuinely gone.
    results = [_dir_result(
        "/Movies/Some.Release-GROUP/", "RESULT_ID_1",
        dupe={"id": "share_full", "paths": ["Z:\\mnt\\zzd\\share\\Movies\\Some.Release-GROUP\\"]},
    )]
    ad = FakeAirDCPP(results)
    outcome = _repair(ad, release_dir, ["some.release-group.r42", "some.release-group.r58"])
    assert outcome["ok"] is True
    assert outcome["queued"] == ["some.release-group.r42", "some.release-group.r58"]
    assert outcome["not_found"] == []
    # Target is the release's PARENT dir — AirDC++ appends the result's own
    # folder name itself. Passing the release dir itself (the old bug) made
    # AirDC++ nest a full duplicate copy inside the existing folder instead
    # of merging into it (caught live 2026-08-28, Jesse Stone/Lilo & Stitch).
    assert ad.queued == [("RESULT_ID_1", "Z:\\Movies\\")]
    assert ad.deleted_instances == [1]


def test_a_differently_named_directory_is_never_trusted():
    release_dir = "/mnt/zzd/share/Movies/Some.Release-GROUP"
    results = [_dir_result("/Movies/Some.Other.Release-DIFFERENT/", "RESULT_ID_1")]
    ad = FakeAirDCPP(results)
    outcome = _repair(ad, release_dir, ["some.release-group.r42"])
    assert outcome["ok"] is True
    assert outcome["queued"] == []
    assert outcome["not_found"] == ["some.release-group.r42"]
    assert ad.queued == []


def test_directory_name_match_is_case_insensitive():
    release_dir = "/mnt/zzd/share/Movies/Some.Release-GROUP"
    results = [_dir_result("/Movies/some.release-group/", "RESULT_ID_1")]
    ad = FakeAirDCPP(results)
    outcome = _repair(ad, release_dir, ["some.release-group.r42"])
    assert outcome["queued"] == ["some.release-group.r42"]


def test_file_type_results_are_never_treated_as_the_directory_match():
    release_dir = "/mnt/zzd/share/Movies/Some.Release-GROUP"
    results = [{
        "path": "/Movies/Some.Release-GROUP/some.release-group.r42",
        "id": "FILE_RESULT_ID", "size": 1000, "type": {"id": "file"}, "dupe": None,
    }]
    ad = FakeAirDCPP(results)
    outcome = _repair(ad, release_dir, ["some.release-group.r42"])
    assert outcome["queued"] == []
    assert outcome["not_found"] == ["some.release-group.r42"]
    assert ad.queued == []


def test_no_matching_directory_on_the_hub_is_reported_not_an_error(tmp_path):
    ad = FakeAirDCPP([])
    outcome = _repair(ad, "/mnt/zzd/share/Movies/Some.Release-GROUP", ["x.r00"])
    assert outcome["ok"] is True
    assert outcome["error"] == "no exact-name hub source found"
    assert outcome["not_found"] == ["x.r00"]


def test_queue_result_failure_is_reported(tmp_path):
    release_dir = "/mnt/zzd/share/Movies/Some.Release-GROUP"
    ad = FakeAirDCPP([_dir_result("/Movies/Some.Release-GROUP/", "RESULT_ID_1")], queue_ok=False)
    outcome = _repair(ad, release_dir, ["some.release-group.r42"])
    assert outcome["ok"] is False
    assert outcome["not_found"] == ["some.release-group.r42"]


def test_a_path_outside_linux_root_is_reported_not_raised(tmp_path):
    # Real crash caught live 2026-08-28: media-audit sent its OWN container
    # path ("/movies-fin/...", meaningless to dc-bridge) straight through,
    # and _to_smb_dir's ValueError propagated as an unhandled 500 instead
    # of a clean error. This is what a caller sending a bad path should
    # get instead — and it must be caught before ever touching AirDC++.
    ad = FakeAirDCPP([])
    outcome = _repair(ad, "/movies-fin/Some.Release-GROUP", ["some.release-group.r00"])
    assert outcome["ok"] is False
    assert "path translation failed" in outcome["error"]
    assert ad.deleted_instances == []  # never even created a search instance


def test_no_missing_files_is_a_no_op_that_never_touches_airdcpp():
    ad = FakeAirDCPP([])
    outcome = _repair(ad, "/mnt/zzd/share/Movies/X", [])
    assert outcome["ok"] is False
    assert ad.deleted_instances == []


def test_search_instance_creation_failure_is_reported_not_raised():
    ad = FakeAirDCPP([], create_instance_ok=False)
    outcome = _repair(ad, "/mnt/zzd/share/Movies/X", ["x.r00"])
    assert outcome["ok"] is False
    assert outcome["not_found"] == ["x.r00"]


def test_hub_search_failure_is_reported_and_still_cleans_up_the_instance():
    ad = FakeAirDCPP([], hub_search_ok=False)
    outcome = _repair(ad, "/mnt/zzd/share/Movies/X", ["x.r00"])
    assert outcome["ok"] is False
    assert ad.deleted_instances == [1]
