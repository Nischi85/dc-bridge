"""Tests for the title/quality/language matching guards in dcbridge/helpers.py.

These are pulled directly from the concrete wrong-grab scenarios documented in
each function's own docstring — the bugs they exist to prevent, not synthetic
edge cases. If one of these starts failing, it means a real release the bridge
used to correctly reject (or correctly accept) has flipped.
"""
from dcbridge.config import QualityCfg
from dcbridge.helpers import (
    _scene_tag_region,
    compile_dub_re,
    configure_filters,
    has_unwanted_subs,
    is_adult_release,
    is_foreign_language,
    movie_title_prefix_ok,
    passes_quality,
    release_matches_title,
    release_matches_year,
    release_starts_with_title,
    sanitize_for_dc_search,
    score_result,
    to_ascii,
    tv_release_extra_words_ok,
    tv_release_matches_year,
)


def _quality(**overrides) -> QualityCfg:
    base = {"episode_size_mb": (100, 5000), "movie_size_mb": (500, 20000)}
    base.update(overrides)
    return QualityCfg(**base)


# ── release_matches_title ────────────────────────────────────────────────────


def test_release_matches_title_rejects_unrelated_show_sharing_a_word():
    # 'Bad Judge' search must not grab a Judge Judy episode.
    assert not release_matches_title("Judge.Judy.S18E81.1080p.WEB", "Bad Judge")


def test_release_matches_title_rejects_title_word_buried_in_episode_title():
    # 'Star City' search must not grab Star Trek Picard's "Stardust City" episode.
    assert not release_matches_title(
        "Star.Trek.Picard.S01E05.Stardust.City.Rag.1080p.WEB", "Star City"
    )


def test_release_matches_title_anchored_accepts_the_real_show():
    assert release_matches_title("Star.City.S01E01.1080p.WEB", "Star City", anchored=True)
    assert release_matches_title("Bad.Judge.S01E01.1080p.WEB", "Bad Judge", anchored=True)


def test_release_matches_title_word_boundary_does_not_match_inside_longer_word():
    # 'star' must not match inside 'stardust'.
    assert not release_matches_title("Stardust.2007.1080p.BluRay", "Star", anchored=True)


# ── tv_release_extra_words_ok (same-titled different series) ────────────────


def test_wallander_rejects_older_film_adaptations():
    # The 2005 series must not grab the older Wallander film adaptations
    # packaged with episode markers.
    assert not tv_release_extra_words_ok(
        "Wallander.Hundarna.I.Riga.S01E01.1080p.WEB", "Wallander", year=2005,
    )
    assert not tv_release_extra_words_ok(
        "Wallander.Villospar.2001.S01E03.1080p.WEB", "Wallander", year=2005,
    )


def test_wallander_accepts_own_year_and_bare_release():
    assert tv_release_extra_words_ok(
        "Wallander.2005.S01E01.1080p.WEB", "Wallander", year=2005,
    )
    assert tv_release_extra_words_ok(
        "Wallander.S01E01.1080p.WEB", "Wallander", year=2005,
    )


def test_extra_words_ok_permissive_with_no_episode_marker():
    assert tv_release_extra_words_ok("Wallander.Something.Else", "Wallander", year=2005)


# ── release_starts_with_title / movie_title_prefix_ok (movies) ──────────────


def test_johan_falk_abbreviation_accepted():
    assert release_starts_with_title(
        "Johan.Falk.GSI.2015.1080p.BluRay", "Johan Falk: GSI - Gruppen"
    )


def test_release_starts_with_title_rejects_mid_name_match():
    # 'Obsession' must not match a release that merely contains the word.
    assert not release_starts_with_title(
        "Roccos.World.Feet.Obsession.2.XXX.1080p", "Obsession"
    )


def test_movie_title_prefix_rejects_same_opening_words_different_film():
    # 'The Odyssey' (2026) must not grab a documentary that merely starts the
    # same way.
    assert not movie_title_prefix_ok(
        "The.Odyssey.with.Dan.Snow.2026.1080p.WEB", "The Odyssey"
    )


def test_movie_title_prefix_accepts_exact_match():
    assert movie_title_prefix_ok("The.Odyssey.2026.1080p.WEB", "The Odyssey")


# ── release_matches_year / tv_release_matches_year ───────────────────────────


def test_movie_yearless_release_rejected_outright():
    # A real movie scene release always carries a year; a yearless title that
    # merely shares a word is junk.
    assert not release_matches_year("The.Odyssey.720p.HDTV", 2026)


def test_movie_sequel_does_not_match_older_same_title_film():
    assert not release_matches_year("The.Devil.Wears.Prada.2006.1080p.BluRay", 2026)


def test_movie_year_within_tolerance_matches():
    assert release_matches_year("Some.Movie.2025.1080p.WEB", 2026, tolerance=1)


def test_tv_yearless_release_is_permissive():
    # Ordinary SxxExx naming legitimately omits the year.
    assert tv_release_matches_year("Some.Show.S03E04.1080p.WEB", 2020)


def test_tv_wrong_year_present_is_rejected():
    # A same-titled remake from a different year is a real signal something's off.
    assert not tv_release_matches_year("Wallander.Villospar.2001.S01E03", 2005)


# ── language / subtitle filters ──────────────────────────────────────────────


def test_foreign_language_tag_after_year_is_rejected():
    assert is_foreign_language("Some.Movie.2020.GERMAN.720p.WEB")


def test_foreign_language_word_in_title_is_not_falsely_rejected():
    # 'Russian' in the TITLE, not a scene tag — must not be flagged.
    assert not is_foreign_language("Russian.Doll.S01E01.1080p.WEB")
    assert not is_foreign_language("The.French.Dispatch.2021.1080p.WEB")


def test_nordic_tags_kept_by_default():
    # Documented deliberate default: Nordic/East-Asian/MULTi are NOT rejected
    # unless the deployment's config explicitly adds them (see this library's
    # own NORWEGIAN addition to reject_dub_tags).
    assert not is_foreign_language("Hakan.Brakan.003.2025.NORWEGiAN.720p.WEB")


def test_configure_filters_can_add_a_deployment_specific_tag():
    # Mirrors the real fix applied tonight: NORWEGIAN added alongside DANSK.
    configure_filters(["DANSK", "NORWEGIAN"], ["DK", "DANiSH"], ["XXX"])
    try:
        assert is_foreign_language("Hakan.Brakan.003.2025.NORWEGiAN.720p.WEB")
        assert is_foreign_language("Some.Show.S01E01.DANSK.720p.WEB")
    finally:
        # Restore defaults so this test can't leak state into others.
        configure_filters([], [], [])
        configure_filters(
            ["GERMAN", "FRENCH", "DANSK"], ["DK", "DANiSH"], ["XXX"]
        )


def test_unwanted_subs_matches_dksubs_variant():
    assert has_unwanted_subs("Deep.Water.2026.Custom.DKsubs.1080p.WEB-DL")


def test_scene_tag_region_excludes_title_before_year_or_episode_marker():
    assert _scene_tag_region("Russian.Doll.S01E01.German.Dub.WEB") == ".German.Dub.WEB"
    assert _scene_tag_region("No.Marker.Here") == "No.Marker.Here"


def test_compile_dub_re_empty_list_matches_nothing():
    assert compile_dub_re([]).search("Anything.GERMAN.here") is None


# ── adult content exemption ───────────────────────────────────────────────────


def test_adult_release_rejected_for_normal_request():
    assert is_adult_release("Roccos.World.Obsession.XXX.1080p", "Obsession")


def test_adult_tag_exempt_when_own_title_carries_it():
    assert not is_adult_release("xXx.2002.1080p.BluRay", "xXx")


# ── quality gating + scoring ──────────────────────────────────────────────────


def test_passes_quality_rejects_outside_size_bounds():
    q = _quality(episode_size_mb=(100, 2000))
    assert not passes_quality("Show.S01E01.1080p.WEB", 50 * 1024 * 1024, "tv", q)
    assert not passes_quality("Show.S01E01.1080p.WEB", 3000 * 1024 * 1024, "tv", q)


def test_passes_quality_priority_tier_must_match():
    q = _quality(priority=["web 1080p", "bluray 1080p"])
    size = 1000 * 1024 * 1024
    assert passes_quality("Show.S01E01.WEB.1080p", size, "tv", q)
    assert not passes_quality("Show.S01E01.HDTV.720p", size, "tv", q)


def test_score_result_prefers_earlier_priority_tier_over_larger_size():
    q = _quality(priority=["web 1080p", "hdtv 720p"])
    web = score_result("Show.S01E01.WEB.1080p", 500 * 1024 * 1024, q)
    hdtv_bigger = score_result("Show.S01E01.HDTV.720p", 5000 * 1024 * 1024, q)
    assert web > hdtv_bigger  # earlier tier always outranks a later, bigger one


def test_score_result_size_is_tiebreak_within_same_tier():
    q = _quality(priority=["web 1080p"])
    small = score_result("Show.S01E01.WEB.1080p", 500 * 1024 * 1024, q)
    big = score_result("Show.S01E01.WEB.1080p", 1500 * 1024 * 1024, q)
    assert big > small


# ── text sanitisation ──────────────────────────────────────────────────────────


def test_sanitize_for_dc_search_folds_nordic_letters_and_strips_apostrophes():
    assert sanitize_for_dc_search("Johan Falk: Alla råns moder") == "Johan Falk Alla rans moder"
    assert sanitize_for_dc_search("He's Just Not That Into You") == "Hes Just Not That Into You"


def test_sanitize_for_dc_search_collapses_ellipsis_that_would_break_hub_search():
    assert "..." not in sanitize_for_dc_search("Someone Like You...")


def test_to_ascii_transliterates_nordic_letters():
    assert to_ascii("Håkan Bråkan") == "Hakan Brakan"
