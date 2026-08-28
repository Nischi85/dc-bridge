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

    async def queue_result(self, iid, tth, target_dir):
        if not self._queue_ok:
            return None
        self.queued.append((tth, target_dir))
        return {"ok": True}

    async def delete_instance(self, iid):
        self.deleted_instances.append(iid)


def _cfg() -> Config:
    return Config.model_construct(path_map=PathMap(linux_root="/mnt/zzd/share", windows_root="Z:\\"))


def _result(path: str, tth: str, is_dir: bool = False, size: int = 1000) -> dict:
    return {
        "path": path,
        "tth": tth,
        "size": size,
        "type": {"id": "directory" if is_dir else "file"},
    }


def _repair(ad, release_dir, missing_files):
    return asyncio.run(repair_release(_cfg(), ad, release_dir, missing_files, settle_seconds=0))


def test_missing_files_found_and_queued():
    release_dir = "/mnt/zzd/share/Movies/Some.Release-GROUP"
    results = [
        _result("/Movies/Some.Release-GROUP/some.release-group.r42", "TTH42"),
        _result("/Movies/Some.Release-GROUP/some.release-group.r58", "TTH58"),
    ]
    ad = FakeAirDCPP(results)
    outcome = _repair(ad, release_dir, ["some.release-group.r42", "some.release-group.r58"])
    assert outcome["ok"] is True
    assert set(outcome["queued"]) == {"some.release-group.r42", "some.release-group.r58"}
    assert outcome["not_found"] == []
    assert {t for t, _ in ad.queued} == {"TTH42", "TTH58"}
    assert ad.deleted_instances == [1]


def test_a_result_from_a_differently_named_release_is_never_trusted():
    # Same filename, but sitting under a DIFFERENT release folder — must be
    # rejected even though the file itself would satisfy the missing-name
    # check, since it's not verifiably the same bytes.
    release_dir = "/mnt/zzd/share/Movies/Some.Release-GROUP"
    results = [_result("/Movies/Some.Other.Release-DIFFERENT/some.release-group.r42", "WRONG_TTH")]
    ad = FakeAirDCPP(results)
    outcome = _repair(ad, release_dir, ["some.release-group.r42"])
    assert outcome["ok"] is True
    assert outcome["queued"] == []
    assert outcome["not_found"] == ["some.release-group.r42"]
    assert ad.queued == []


def test_release_folder_match_is_case_insensitive():
    release_dir = "/mnt/zzd/share/Movies/Some.Release-GROUP"
    results = [_result("/Movies/some.release-group/some.release-group.r42", "TTH42")]
    ad = FakeAirDCPP(results)
    outcome = _repair(ad, release_dir, ["some.release-group.r42"])
    assert outcome["queued"] == ["some.release-group.r42"]


def test_directory_results_are_never_treated_as_individual_files():
    release_dir = "/mnt/zzd/share/Movies/Some.Release-GROUP"
    results = [_result("/Movies/Some.Release-GROUP", "DIR_TTH", is_dir=True)]
    ad = FakeAirDCPP(results)
    outcome = _repair(ad, release_dir, ["some.release-group.r42"])
    assert outcome["queued"] == []
    assert outcome["not_found"] == ["some.release-group.r42"]


def test_a_file_missing_from_all_hub_results_is_reported_not_found():
    release_dir = "/mnt/zzd/share/Movies/Some.Release-GROUP"
    ad = FakeAirDCPP([_result("/Movies/Some.Release-GROUP/some.release-group.r42", "TTH42")])
    outcome = _repair(ad, release_dir, ["some.release-group.r42", "some.release-group.r99"])
    assert outcome["queued"] == ["some.release-group.r42"]
    assert outcome["not_found"] == ["some.release-group.r99"]


def test_queue_result_failure_reports_the_file_as_not_found():
    release_dir = "/mnt/zzd/share/Movies/Some.Release-GROUP"
    ad = FakeAirDCPP([_result("/Movies/Some.Release-GROUP/some.release-group.r42", "TTH42")], queue_ok=False)
    outcome = _repair(ad, release_dir, ["some.release-group.r42"])
    assert outcome["queued"] == []
    assert outcome["not_found"] == ["some.release-group.r42"]


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
