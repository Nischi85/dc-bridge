"""Miniseries "Part N" naming (match.miniseries_part_numbering): releases like
"Lonesome.Dove.1989.Part1.1080p.BluRay.x264-SSF" carry no SxxExx marker, so a
single-season series maps Part N to S01E0N."""
import asyncio

from dcbridge import poller
from dcbridge.helpers import is_single_season, part_episode_keys, tv_release_extra_words_ok

from tests.test_language_title_fallback import QueryAd
from tests.test_release_candidates import _cfg, _dir_result

YEARS = {"S01E01": 1989, "S01E02": 1989, "S01E03": 1989, "S01E04": 1989}


def _item(years=YEARS):
    return {"id": "sonarr:848", "kind": "tv", "title": "Lonesome Dove", "year": 1989,
            "target_dir_fs": "/mnt/zzd/share/fin/TV.Series", "quality_priority": [],
            "alt_titles": [], "episode_air_years": years}


def _results():
    names = [
        "Lonesome.Dove.1989.Part1.1080p.BluRay.x264-SSF",
        "Lonesome.Dove.Part.2.1989.NORDIC.1080p.BluRay.AC3.5.1.x264-DiRTYBURGER",
        "Lonesome.Dove.1989.Part.3.720p.BluRay.x264-SiNNERS",
        "Lonesome.Dove.Collection.1989.Part.1-4.720p.BluRay.x264-aMa",  # a pack, not a part
        "Lonesome.Dove.The.Outlaw.Years.Part.1.720p.BluRay.x264-X",      # different series
        "Lonesome.Dove.Church.2014.Part.1.1080p.BluRay.x264-RWP",         # different work
    ]
    return [_dir_result(n, f"/Series/{n}/", 7000, f"d{i}") for i, n in enumerate(names)]


def test_part_episode_keys():
    assert part_episode_keys("Lonesome.Dove.1989.Part1.1080p.BluRay.x264-SSF") == ["S01E01"]
    assert part_episode_keys("Lonesome.Dove.Part.4.INTERNAL.DVDRip.XviD-RUNNER") == ["S01E04"]
    assert part_episode_keys("Lonesome.Dove.Collection.1989.Part.1-4.iNTERNAL.DVDRip.XViD-aMa") == []
    assert part_episode_keys("Lonesome.Dove.1989.1080p.BluRay.x264-SSF") == []


def test_is_single_season_ignores_specials():
    assert is_single_season(["S00E01", "S01E01", "S01E02"])
    assert not is_single_season(["S01E01", "S02E01"])
    assert not is_single_season([])


def test_extra_words_guard_with_part_marker():
    ok = lambda n: tv_release_extra_words_ok(n, "Lonesome Dove", 1989, part_marker=True)
    assert ok("Lonesome.Dove.1989.Part1.1080p.BluRay.x264-SSF")
    assert ok("Lonesome.Dove.Part.2.1989.NORDIC.1080p.BluRay-X")
    assert not ok("Lonesome.Dove.The.Outlaw.Years.Part.1.720p.BluRay-X")


def test_select_candidates_maps_parts_for_a_miniseries():
    item = _item()
    out = poller._select_candidates(_results(), "tv", item["title"], item, _cfg(), [],
                                    set(YEARS), item["id"])
    assert {k: [g["release_name"] for g in v] for k, v in out.items()} == {
        "S01E01": ["Lonesome.Dove.1989.Part1.1080p.BluRay.x264-SSF"],
        "S01E02": ["Lonesome.Dove.Part.2.1989.NORDIC.1080p.BluRay.AC3.5.1.x264-DiRTYBURGER"],
        "S01E03": ["Lonesome.Dove.1989.Part.3.720p.BluRay.x264-SiNNERS"],
    }


def test_select_candidates_ignores_parts_for_a_multi_season_show():
    item = _item(dict(YEARS, S02E01=1990))
    assert poller._select_candidates(_results(), "tv", item["title"], item, _cfg(), [],
                                     set(YEARS), item["id"]) == {}


def test_select_candidates_ignores_parts_when_disabled():
    cfg = _cfg()
    cfg.match.miniseries_part_numbering = False
    item = _item()
    assert poller._select_candidates(_results(), "tv", item["title"], item, cfg, [],
                                     set(YEARS), item["id"]) == {}


def test_listing_falls_back_to_a_part_search():
    ad = QueryAd({"Lonesome Dove Part": _results()})
    out = asyncio.run(poller.list_release_candidates(_cfg(), ad, _item(), "S01E03", wait=0))
    assert [c["release_name"] for c in out] == ["Lonesome.Dove.1989.Part.3.720p.BluRay.x264-SiNNERS"]
    assert any(q.endswith("S01E03") for q in ad.hub_searches)
