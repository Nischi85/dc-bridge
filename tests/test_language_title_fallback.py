"""Language-aware alt-title fallback: when a language rule applies and the
canonical title only finds less-preferred-language releases, the ranked
alt titles are searched too (e.g. Swedish dubs of "Philosopher's Stone"
are scene-named after the US "Sorcerer's Stone")."""
import asyncio

from dcbridge.config import LanguageRule
from dcbridge.helpers import has_preferred_language, rank_alt_titles
from dcbridge import poller

from tests.test_release_candidates import FakeAd, FakeState, _cfg, _dir_result


HP_ALTS = [
    "Hari Poter i Kamen mudrosti",
    "ہیری پوٹر اور فلسفی کا پتھر",
    "O Chári Póter kai i Filosofikí Líthos",
    "Harry Potter and the Sorcerer's Stone",
    "해리포터와 마법사의 돌",
    "Harry Potter dhe Guri Filozofal",
]

ENGLISH = "Harry.Potter.And.The.Philosophers.Stone.2001.720p.BluRay.x264-CRF"
SWEDISH = "Harry.Potter.and.The.Sorcerers.Stone.2001.SWEDiSH.720p.BluRay.x264-FARGIRENIS"


class QueryAd(FakeAd):
    """FakeAd whose results depend on the query — first matching substring wins."""

    def __init__(self, by_query):
        super().__init__([])
        self._by_query = by_query
        self._last_query = ""

    async def hub_search(self, iid, query, extensions=None, file_type=None):
        self._last_query = query
        return await super().hub_search(iid, query, extensions, file_type)

    async def get_results(self, iid, start, count):
        for needle, results in self._by_query.items():
            if needle in self._last_query:
                return results
        return []


def _hp_item():
    return {"id": "radarr:4374", "kind": "movie", "title": "Harry Potter and the Philosopher's Stone",
            "year": 2001, "target_dir_fs": "/mnt/zzd/share/fin/TV.For.Children/Movies",
            "quality_priority": [], "alt_titles": HP_ALTS}


def _kids_cfg():
    return _cfg(language_priority=[LanguageRule(path_contains="TV.For.Children", languages=["swedish", "english"])])


def _hp_ad():
    return QueryAd({
        "Philosophers Stone": [_dir_result(ENGLISH, f"/Movies/{ENGLISH}/", 7000, "d1")],
        "Sorcerers Stone": [_dir_result(SWEDISH, f"/Movies/{SWEDISH}/", 7000, "d2")],
    })


def test_rank_alt_titles_puts_latin_high_overlap_first():
    ranked = rank_alt_titles("Harry Potter and the Philosopher's Stone", HP_ALTS)
    assert ranked[0] == "Harry Potter and the Sorcerer's Stone"
    # Non-Latin titles go last.
    assert set(ranked[-2:]) == {"ہیری پوٹر اور فلسفی کا پتھر", "해리포터와 마법사의 돌"}


def test_has_preferred_language():
    assert has_preferred_language([ENGLISH], [])
    assert not has_preferred_language([ENGLISH], ["swedish", "english"])
    assert has_preferred_language([ENGLISH, SWEDISH], ["swedish", "english"])


def test_listing_falls_through_to_an_alt_title_with_the_preferred_language():
    ad = _hp_ad()
    out = asyncio.run(poller.list_release_candidates(_kids_cfg(), ad, _hp_item(), "movie", wait=0))
    assert [c["release_name"] for c in out] == [SWEDISH]
    # Stopped at Sorcerer's Stone (ranked first among alts) — no further searches.
    assert any("Sorcerers Stone" in q for q in ad.hub_searches)
    assert not any("Kamen" in q for q in ad.hub_searches)


def test_listing_keeps_canonical_when_it_already_has_the_preferred_language():
    ad = QueryAd({"Philosophers Stone": [_dir_result(SWEDISH.replace("Sorcerers", "Philosophers"), "/M/x/", 7000, "d1")]})
    out = asyncio.run(poller.list_release_candidates(_kids_cfg(), ad, _hp_item(), "movie", wait=0))
    assert len(out) == 1
    assert all("Philosophers Stone" in q for q in ad.hub_searches)


def test_listing_without_a_language_rule_stops_at_the_canonical_title():
    ad = _hp_ad()
    item = dict(_hp_item(), target_dir_fs="/mnt/zzd/share/fin/Movies")
    out = asyncio.run(poller.list_release_candidates(_kids_cfg(), ad, item, "movie", wait=0))
    assert [c["release_name"] for c in out] == [ENGLISH]
    assert all("Philosophers Stone" in q for q in ad.hub_searches)


def test_listing_falls_back_to_canonical_when_no_variant_has_the_preferred_language():
    ad = QueryAd({"Philosophers Stone": [_dir_result(ENGLISH, f"/Movies/{ENGLISH}/", 7000, "d1")]})
    out = asyncio.run(poller.list_release_candidates(_kids_cfg(), ad, _hp_item(), "movie", wait=0))
    assert [c["release_name"] for c in out] == [ENGLISH]


def test_select_cache_miss_finds_a_release_only_listed_under_an_alt_title():
    ad = _hp_ad()
    queued = asyncio.run(poller.select_release(_kids_cfg(), FakeState(), ad, _hp_item(), "movie", SWEDISH))
    assert queued == 1
    assert ad.queued and ad.queued[0][0] == "d2"
