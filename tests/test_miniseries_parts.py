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
            "target_dir_fs": "/mnt/zzd/share/fin/TV.Series/Lonesome.Dove", "quality_priority": [],
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


# --- bare "E01" naming + match.extra_search_titles -------------------------

SEACROW = {f"S01E{n:02d}": 1964 for n in range(1, 14)}


def _seacrow_item(years=SEACROW):
    return {"id": "sonarr:646", "kind": "tv", "title": "Life on Seacrow Island", "year": 1964,
            "target_dir_fs": "/mnt/zzd/share/fin/TV.For.Children/Series/Vi.Pa.Saltkrakan", "quality_priority": [],
            "alt_titles": ["Ferien auf der Kraeheninsel"], "episode_air_years": years}


def _seacrow_cfg():
    cfg = _cfg()
    cfg.match.extra_search_titles = {"sonarr:646": ["Vi på Saltkråkan"]}
    return cfg


def _seacrow_results():
    names = [
        "Vi.Pa.Saltkrakan.E01.SWEDiSH.1080p.BluRay.x264-CLASSiC",
        "Vi.Pa.Saltkrakan.E02.SWEDiSH.720p.BluRay.x264-CLASSiC",
        "Vi.Pa.Saltkrakan.Filmen.E01.SWEDiSH.720p.BluRay.x264-X",  # different work
    ]
    return [_dir_result(n, f"/Series/{n}/", 7000, f"s{i}") for i, n in enumerate(names)]


class ExactQueryAd(QueryAd):
    """Results only for an exact (case-insensitive, like the hub) query — the
    hub's AND-of-terms match means a "<title> S01E" query can't return a
    season-less "Title.E01" folder."""

    async def get_results(self, iid, start, count):
        return {q.lower(): r for q, r in self._by_query.items()}.get(self._last_query.lower(), [])


def test_bare_episode_keys():
    from dcbridge.helpers import bare_episode_keys, single_season_episode_keys
    assert bare_episode_keys("Vi.Pa.Saltkrakan.E07.SWEDiSH.1080p.BluRay.x264-CLASSiC") == ["S01E07"]
    assert bare_episode_keys("Show.S02E07.1080p.WEB.h264-X") == []
    assert bare_episode_keys("Show.1080p.WEB.DDP5.1.E-AC3.h264-X") == []
    assert single_season_episode_keys("Lonesome.Dove.1989.Part1.1080p.BluRay.x264-SSF") == ["S01E01"]
    assert single_season_episode_keys("Vi.Pa.Saltkrakan.E13.SWEDiSH.1080p.BluRay.x264-X") == ["S01E13"]


def test_extra_words_guard_with_bare_episode_marker():
    ok = lambda n: tv_release_extra_words_ok(n, "Vi Pa Saltkrakan", 1964, part_marker=True)
    assert ok("Vi.Pa.Saltkrakan.E01.SWEDiSH.1080p.BluRay.x264-CLASSiC")
    assert not ok("Vi.Pa.Saltkrakan.Filmen.E01.SWEDiSH.720p.BluRay.x264-X")


def test_select_candidates_maps_bare_episodes_for_a_single_season_series():
    item = _seacrow_item()
    out = poller._select_candidates(_seacrow_results(), "tv", "Vi på Saltkråkan", item, _seacrow_cfg(), [],
                                    set(SEACROW), item["id"])
    assert {k: [g["release_name"] for g in v] for k, v in out.items()} == {
        "S01E01": ["Vi.Pa.Saltkrakan.E01.SWEDiSH.1080p.BluRay.x264-CLASSiC"],
        "S01E02": ["Vi.Pa.Saltkrakan.E02.SWEDiSH.720p.BluRay.x264-CLASSiC"],
    }


def test_select_candidates_ignores_bare_episodes_for_a_multi_season_show():
    item = _seacrow_item(dict(SEACROW, S02E01=1965))
    assert poller._select_candidates(_seacrow_results(), "tv", "Vi på Saltkråkan", item, _seacrow_cfg(), [],
                                     set(SEACROW), item["id"]) == {}


def test_listing_title_variants_put_extra_titles_first():
    assert poller._listing_title_variants(_seacrow_cfg(), _seacrow_item()) == [
        "Vi på Saltkråkan", "Life on Seacrow Island", "Ferien auf der Kraeheninsel",
    ]
    # No config entry -> the folder title ("…/Vi.Pa.Saltkrakan") stands in for it.
    assert poller._listing_title_variants(_cfg(), _seacrow_item()) == [
        "Vi Pa Saltkrakan", "Life on Seacrow Island", "Ferien auf der Kraeheninsel",
    ]


def test_episode_listing_finds_bare_episode_via_extra_title():
    ad = ExactQueryAd({"Vi Pa Saltkrakan": _seacrow_results()})
    out = asyncio.run(poller.list_release_candidates(_seacrow_cfg(), ad, _seacrow_item(), "S01E02", wait=0))
    assert [c["release_name"] for c in out] == ["Vi.Pa.Saltkrakan.E02.SWEDiSH.720p.BluRay.x264-CLASSiC"]
    # The configured title goes first and its bare-title fallback hits.
    assert ad.hub_searches == ["Vi pa Saltkrakan S01E02", "Vi pa Saltkrakan E02", "Vi pa Saltkrakan"]


def test_episode_listing_tries_a_targeted_bare_episode_query_first():
    # A bare-title search of a long series is truncated by hub result caps;
    # "<title> E12" reaches an episode past the cut.
    e12 = [_dir_result("Vi.Pa.Saltkrakan.E12.SWEDiSH.1080p.BluRay.x264-CLASSiC", "/S/e12/", 7000, "e12")]
    ad = ExactQueryAd({"Vi Pa Saltkrakan E12": e12, "Vi Pa Saltkrakan": _seacrow_results()})
    out = asyncio.run(poller.list_release_candidates(_seacrow_cfg(), ad, _seacrow_item(), "S01E12", wait=0))
    assert [c["release_name"] for c in out] == ["Vi.Pa.Saltkrakan.E12.SWEDiSH.1080p.BluRay.x264-CLASSiC"]


def test_listing_collapses_copies_of_one_release_from_several_users():
    name = "Vi.Pa.Saltkrakan.E02.SWEDiSH.720p.BluRay.x264-CLASSiC"
    copies = [_dir_result(name, f"/u{i}/{name}/", 7000 - i, f"c{i}") for i in range(3)]
    ad = ExactQueryAd({"Vi Pa Saltkrakan E02": copies})
    out = asyncio.run(poller.list_release_candidates(_seacrow_cfg(), ad, _seacrow_item(), "S01E02", wait=0))
    assert [c["release_name"] for c in out] == [name]


def test_bare_title_fallback_isnt_cut_short_by_old_part_rips():
    # The live hub has old "Vi.Pa.Saltkrakan.Part1.1963..." SVCD rips beside the
    # "E01".."E13" BluRay ones: the bare-title search sees both kinds.
    parts = [_dir_result(f"Vi.Pa.Saltkrakan.Part{n}.1963.720p.DVDRip-X", f"/S/p{n}/", 7000, f"p{n}")
             for n in (1, 2, 3)]
    ad = ExactQueryAd({"Vi Pa Saltkrakan": parts + _seacrow_results(),
                       "Vi Pa Saltkrakan Part": parts})
    out = asyncio.run(poller.list_season_candidates(
        _seacrow_cfg(), ad, _seacrow_item(), 1, list(SEACROW), wait=0,
    ))
    assert sorted(c["release_name"] for c in out["S01E02"]) == [
        "Vi.Pa.Saltkrakan.E02.SWEDiSH.720p.BluRay.x264-CLASSiC", "Vi.Pa.Saltkrakan.Part2.1963.720p.DVDRip-X",
    ]
    assert set(out) == {"S01E01", "S01E02", "S01E03"}


def test_season_listing_falls_back_to_a_bare_title_search():
    ad = ExactQueryAd({"Vi Pa Saltkrakan": _seacrow_results()})
    out = asyncio.run(poller.list_season_candidates(
        _seacrow_cfg(), ad, _seacrow_item(), 1, list(SEACROW), wait=0,
    ))
    assert {k: [c["release_name"] for c in v] for k, v in out.items()} == {
        "S01E01": ["Vi.Pa.Saltkrakan.E01.SWEDiSH.1080p.BluRay.x264-CLASSiC"],
        "S01E02": ["Vi.Pa.Saltkrakan.E02.SWEDiSH.720p.BluRay.x264-CLASSiC"],
    }
    assert "Vi pa Saltkrakan S01E" in ad.hub_searches
    assert "Vi pa Saltkrakan" in ad.hub_searches


# --- the series folder's own title as a listing search title ----------------

def _moomin_item(folder="I.Mumindalen", title="Moomin"):
    return {"id": "sonarr:626", "kind": "tv", "title": title, "year": 1990,
            "target_dir_fs": f"/mnt/zzd/share/fin/TV.For.Children/Series/{folder}", "quality_priority": [],
            "alt_titles": ["Mumins 1990"], "episode_air_years": {"S01E01": 1990, "S02E01": 1991}}


def test_folder_title_is_searched_before_the_canonical_title():
    assert poller._listing_title_variants(_cfg(), _moomin_item()) == ["I Mumindalen", "Moomin", "Mumins 1990"]


def test_folder_title_that_only_differs_in_noise_is_not_added():
    for folder, title in [("Paradise", "Paradise (2025)"), ("Superman.And.Lois", "Superman & Lois"),
                          ("Shameless", "Shameless (US)"), ("Moomin", "Moomin")]:
        assert poller._listing_title_variants(_cfg(), _moomin_item(folder, title))[0] == title, folder


def test_folder_named_after_a_whole_release_is_cut_to_its_title():
    item = _moomin_item("Skrotnisse.Och.Hans.Vanner.1985.SWEDiSH.PROPER.DVDRip.XviD-iLS", "Skrotnisse och hans vänner")
    assert poller._folder_title(item) == "Skrotnisse Och Hans Vanner"
    assert poller._listing_title_variants(_cfg(), item)[0] == "Skrotnisse och hans vänner"


def test_folder_title_is_not_used_for_movies_or_polls():
    movie = dict(_moomin_item(), kind="movie", id="radarr:1")
    assert poller._folder_title(movie) == ""
    assert poller._extra_search_titles(_cfg(), _moomin_item()) == []


def test_season_listing_finds_releases_under_the_folder_title():
    names = ["I.Mumindalen.S01E01.SWE.ENG.720p.WEB.H264-X", "Moomin.Valley.S01E01.1080p.WEB.H264-Y"]
    ad = ExactQueryAd({"I Mumindalen S01E": [_dir_result(n, f"/S/{n}/", 7000, n) for n in names]})
    out = asyncio.run(poller.list_season_candidates(_cfg(), ad, _moomin_item(), 1, ["S01E01"], wait=0))
    assert [c["release_name"] for c in out["S01E01"]] == ["I.Mumindalen.S01E01.SWE.ENG.720p.WEB.H264-X"]
    assert ad.hub_searches[0] == "I Mumindalen S01E"


def test_season_listing_re_asks_per_block_of_ten_for_episodes_a_capped_search_missed():
    def res(*eps):
        return [_dir_result(f"I.Mumindalen.S01E{e:02d}.SWE.1080p.WEB.H264-X", f"/S/{e}/", 7000, f"e{e}") for e in eps]
    ad = ExactQueryAd({
        "I Mumindalen S01E": res(1, 2, 3),       # cut short by hub result caps
        "I Mumindalen S01E1": res(10, 11, 12),
        "I Mumindalen S01E2": res(21),
    })
    keys = [f"S01E{e:02d}" for e in (1, 2, 3, 11, 12, 21, 22)]
    poller._CANDIDATE_CACHE.clear()  # an earlier test's sonarr:626 entries would be evicted (deleted) here
    out = asyncio.run(poller.list_season_candidates(_cfg(), ad, _moomin_item(), 1, keys, wait=0))
    assert sorted(out) == ["S01E01", "S01E02", "S01E03", "S01E11", "S01E12", "S01E21"]
    assert ad.hub_searches == ["I Mumindalen S01E", "I Mumindalen S01E1", "I Mumindalen S01E2"]
    # each block's instance is cached under its own episodes and stays open
    cache = poller._CANDIDATE_CACHE
    assert cache["sonarr:626:S01E11"]["iid"] == cache["sonarr:626:S01E12"]["iid"] != cache["sonarr:626:S01E01"]["iid"]
    assert ad.instances_deleted == []


def test_block_follow_up_for_season_less_naming_uses_bare_episode_terms():
    def res(*eps):
        return [_dir_result(f"Vi.Pa.Saltkrakan.E{e:02d}.SWEDiSH.1080p.BluRay.x264-CLASSiC", f"/S/{e}/", 7000, f"v{e}")
                for e in eps]
    ad = ExactQueryAd({"Vi Pa Saltkrakan": res(1, 2), "Vi Pa Saltkrakan E1": res(11, 12, 13)})
    out = asyncio.run(poller.list_season_candidates(_seacrow_cfg(), ad, _seacrow_item(), 1, list(SEACROW), wait=0))
    assert {"S01E01", "S01E11", "S01E13"} <= set(out)
    assert "Vi pa Saltkrakan E1" in ad.hub_searches
