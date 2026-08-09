"""Pure helpers: HTTP parsing, scene-name sanitisation, quality/title/year matching,
scheduling cadence, result scoring."""
from __future__ import annotations
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional
import httpx
from dcbridge.config import Config, QualityCfg
log = logging.getLogger("dc_bridge")


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return r.text


def _truncate(s: str, n: int = 400) -> str:
    return s if len(s) <= n else s[:n] + "...(truncated)"


# ── Quality / matching ───────────────────────────────────────────────────────


_EPISODE_RE = re.compile(r"\bS(\d{1,2})E(\d{1,3})\b", re.I)

# Season/episode markers: SxxExx, bare Sxx season packs, or "Season N" wording.
_SEASON_OR_EP_RE = re.compile(r"\b(?:S\d{1,2}(?:E\d{1,3})?|Season[. _]?\d{1,2})\b", re.I)


_TRANSLIT = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss",
    "ð": "d", "Ð": "D", "þ": "th", "Þ": "TH", "ł": "l", "Ł": "L",
})


def to_ascii(s: str) -> str:
    """Transliterate accented / Nordic letters to ASCII (å/ä->a, ö->o, é->e,
    ø->o, æ->ae, …). Scene releases never use non-ASCII characters — a Swedish
    title like "Alla råns moder" is released as "Alla.Rans.Moder" — so both the
    hub query and title matching must fold to ASCII to line up with them."""
    s = s.translate(_TRANSLIT)
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def sanitize_for_dc_search(s: str) -> str:
    """Normalise a title for a DC++ hub search.

    Folds non-ASCII letters to ASCII (scene names do too), strips apostrophes
    entirely (scene names omit them: "He's" -> "Hes") and replaces anything that
    isn't alphanumeric / dot / dash / underscore / space with a single space.
    Then collapses runs of whitespace. The result is a space-tokenized query
    whose tokens substring-match scene-named files (which use dots between words),
    and avoids characters most DC hubs reject or treat as field operators.

    Examples:
      "Demon Slayer: Kimetsu no Yaiba Infinity Castle"
                              -> "Demon Slayer Kimetsu no Yaiba Infinity Castle"
      "He's Just Not That Into You"  -> "Hes Just Not That Into You"
      "Johan Falk: Alla råns moder"  -> "Johan Falk Alla rans moder"
    """
    if not s:
        return s
    # Fold accented/Nordic letters to ASCII to match scene naming.
    s = to_ascii(s)
    # Apostrophes: remove (no space). Both straight and curly variants.
    s = s.replace("'", "").replace("’", "").replace("‘", "")
    # Everything else not in the safe set becomes a space.
    s = re.sub(r"[^\w\s.\-]", " ", s, flags=re.UNICODE)
    # Runs of 2+ dots are title punctuation (an ellipsis, e.g. "Someone Like
    # You...") rather than the single dots scene names use between words —
    # collapse to a space. A literal "..." left in the query breaks DC++ hub
    # search outright (zero results from every hub, even though the same
    # query without it matches hundreds) since no real filename ever
    # contains three consecutive dots.
    s = re.sub(r"\.{2,}", " ", s)
    # Collapse whitespace.
    s = re.sub(r"\s+", " ", s).strip()
    return s


_SUBTITLE_SEPARATOR_RE = re.compile(r"\s*[:–—]\s+")  # ": ", " – ", " — "


def title_before_subtitle_separator(title: str) -> Optional[str]:
    """The portion of `title` before its first colon/dash subtitle separator, e.g.
    "Trustorhärvan – verkligheten bakom Golden Boys" -> "Trustorhärvan". Tried as an
    extra search variant: AirDC++ AND-matches every space-separated query term, so a
    query built from the FULL title requires a documentary/subtitle's descriptive tail
    to appear in the release too — but scene releases commonly drop that tail entirely
    and keep only the short form. None if `title` has no such separator, or nothing
    meaningful precedes it."""
    m = _SUBTITLE_SEPARATOR_RE.search(title)
    if not m:
        return None
    prefix = title[: m.start()].strip()
    return prefix or None


def loosen_hyphens_for_search(s: str) -> str:
    """Split a sanitized string's internal hyphens into separate search terms
    (query-construction only — never used for folder naming, which must keep
    a title's real hyphens intact).

    AirDC++ AND-matches each space-separated query term as its own substring
    of the release name/path, independent of what's adjacent to it. So a
    hyphenated compound in the source title (e.g. TMDB's alternate title
    "EM-krönika") only needs "EM" and "kronika" to each appear somewhere in
    the release — it doesn't require them to be joined by a literal hyphen,
    which a scene release may render differently (dot-separated, or not
    hyphenated at all, e.g. "EM.Kronika"). Keeping the hyphen as one literal
    token would make an otherwise-correct query miss such a release entirely;
    splitting it only loosens the hub query, and the existing title-match
    guards (release_starts_with_title, movie_title_prefix_ok,
    release_matches_title) still enforce correctness on the results."""
    return re.sub(r"-", " ", s)


def title_to_folder_name(title: str) -> str:
    """A scene-style, dot-separated folder name for `title` (e.g. "Hem till
    Midgård" -> "Hem.till.Midgard"). Reuses sanitize_for_dc_search's ASCII-fold
    / apostrophe-strip so a series/movie folder name always matches the same
    charset scene releases (and the rest of this library) use."""
    return sanitize_for_dc_search(title).replace(" ", ".")


_UNSAFE_FOLDER_CHAR_RE = re.compile(r"[^A-Za-z0-9._-]")


def folder_name_is_scene_safe(name: str) -> bool:
    """True if `name` contains only scene-safe characters (alnum, dot, dash,
    underscore) — i.e. no non-ASCII letters, spaces, or punctuation Sonarr's
    own folder naming might leave in (e.g. "Midgård")."""
    return not _UNSAFE_FOLDER_CHAR_RE.search(name)


def episode_keys_from_name(name: str) -> list[str]:
    """Return ['S03E04', ...] for any episode markers in `name`."""
    return [f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}" for m in _EPISODE_RE.finditer(name)]


def season_of_episode_key(key: str) -> Optional[int]:
    """The season number of a 'SxxExx' key (e.g. "S05E01" -> 5), or None."""
    m = re.match(r"S(\d{1,2})E\d", key, re.I)
    return int(m.group(1)) if m else None


# Map Sonarr/Radarr quality `source` values to the substring that appears in a
# scene release name. Sonarr says "television", Radarr says "tv" — both -> hdtv.
_QUALITY_SOURCE_TOKEN = {
    "tv": "hdtv", "television": "hdtv", "televisionraw": "hdtv",
    "web": "web", "webdl": "web", "webrip": "web",
    "bluray": "bluray", "blurayraw": "bluray", "dvd": "dvd",
}


def profile_to_priority(profile: dict) -> list[str]:
    """Turn a Sonarr/Radarr quality profile into an ordered, MOST-PREFERRED-FIRST
    list of "<source> <resolution>" match specs, e.g. ['web 720p', 'web 1080p',
    'hdtv 720p', 'hdtv 1080p', 'bluray 720p', 'bluray 1080p']. *arr lists profile
    items lowest->highest, so we reverse. Quality groups (WEB = WEBDL/WEBRip)
    collapse to one 'web <res>' spec. Only allowed qualities with a mappable
    source + resolution are kept (SD/cam/etc. drop out)."""
    specs: list[str] = []
    for it in reversed(profile.get("items", []) or []):
        if not it.get("allowed"):
            continue
        q = it.get("quality")
        if q:
            src = _QUALITY_SOURCE_TOKEN.get((q.get("source") or "").lower())
            res = q.get("resolution")
        elif it.get("items"):  # a group like "WEB 720p"
            first = (it["items"][0] or {}).get("quality") or {}
            src = _QUALITY_SOURCE_TOKEN.get((first.get("source") or "").lower())
            res = first.get("resolution")
        else:
            continue
        if src:
            # DVD releases (DVDRip/XviD) never carry a resolution token in
            # scene names, even though Sonarr's DVD quality reports
            # resolution=480 -- match the source alone. WEB/HDTV/Bluray
            # scene names do carry a literal resolution token (720p/1080p/...).
            spec = src if src == "dvd" else (f"{src} {res}p" if res else src)
            if spec not in specs:
                specs.append(spec)
    return specs


async def _fetch_quality_profiles(
    url: str, headers: dict, http: httpx.AsyncClient
) -> tuple[dict, dict]:
    """(by_id, by_name) maps of profileId / profile-name -> ['web 720p', ...] for a
    Sonarr/Radarr instance. Both empty on error so the bridge falls back to the
    config quality rules. by_name lets the bridge resolve quality.profile_name."""
    try:
        r = await http.get(f"{url}/api/v3/qualityprofile", headers=headers)
        if r.status_code == 200:
            profiles = r.json()
            by_id = {p["id"]: profile_to_priority(p) for p in profiles}
            by_name = {p.get("name"): profile_to_priority(p) for p in profiles}
            return by_id, by_name
    except Exception as e:
        log.debug("could not fetch quality profiles from %s: %s", url, e)
    return {}, {}


def _priority_rank(name_l: str, priority: list[str]) -> Optional[int]:
    """Index of the first priority entry whose space-separated tokens are ALL
    present (substring) in name_l — lower = more preferred; None = no match."""
    for i, entry in enumerate(priority):
        if all(t in name_l for t in entry.lower().split()):
            return i
    return None


def passes_quality(
    name: str, size_bytes: int, kind: str, quality: QualityCfg,
    priority: Optional[list[str]] = None,
) -> bool:
    name_l = name.lower()
    lo, hi = (quality.episode_size_mb if kind == "tv" else quality.movie_size_mb)
    mb = size_bytes / (1024 * 1024) if size_bytes else 0
    if not (lo <= mb <= hi):
        return False
    pri = priority or quality.priority
    if pri:  # *arr profile (per item) or config `priority`: accept iff it matches a tier
        return _priority_rank(name_l, pri) is not None
    # Legacy fallback: an accepted source keyword AND an accepted resolution.
    if not any(k.lower() in name_l for k in quality.accepted_keywords):
        return False
    if not any(r.lower() in name_l for r in quality.resolutions):
        return False
    return True


_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def release_matches_year(name: str, want_year: int | None, tolerance: int = 1) -> bool:
    """True if `name` carries a year within ±tolerance of `want_year`.

    Guards movie matches two ways: a sequel request ("The Devil Wears Prada 2",
    2026) won't grab the same-title older film ("...Prada 2006"); and a release
    with NO year is rejected outright — a real movie scene release always carries
    its year, so a yearless title that merely shares a word (e.g. "The Odyssey"
    matching "...A.Summer.Odyssey.720p.HDTV") is junk. Only permissive when we
    have no requested year to compare against.
    """
    if not want_year:
        return True
    years = [int(y) for y in _YEAR_RE.findall(name)]
    if not years:
        return False
    return any(abs(y - want_year) <= tolerance for y in years)


def tv_release_matches_year(release_name: str, want_year: Optional[int], tolerance: int = 1) -> bool:
    """True if `release_name` carries no year, OR a year within ±tolerance of
    `want_year` (the SPECIFIC episode's broadcast year, not the show's start
    year — a long-running series spans many years, so a per-item year would be
    too coarse). Unlike release_matches_year (movies), a yearless TV release is
    NOT rejected — standard SxxExx scene naming legitimately omits the year for
    ordinary episodes; only a PRESENT, wrong year is a signal something's off
    (e.g. a same-titled remake produced in a different year, 'Wallander...2008'
    for a 2005-aired episode). Permissive when want_year is unknown."""
    if not want_year:
        return True
    years = [int(y) for y in _YEAR_RE.findall(release_name)]
    if not years:
        return True
    return any(abs(y - want_year) <= tolerance for y in years)


_SD_SOURCE_RE = re.compile(r"\b(?:dvdrip|dvdscr|dvd-?r|dvd|xvid|divx|sdtv|vhsrip|tvrip)\b", re.I)


def is_sd_release(name: str) -> bool:
    """A DVD/SD-source release (DVDRip, XviD, …). These legitimately omit the
    year, so the yearless-junk year-guard is relaxed for them."""
    return bool(_SD_SOURCE_RE.search(name))


# Two denylists, both driven by the `filters` block in config.yaml: foreign-DUB
# scene tags, and subtitle-language stems for English-audio releases muxed with
# unwanted subs. The defaults below reproduce the historical hardcoded sets and
# stay in effect until configure_filters() runs at startup. A tag NOT listed is
# kept — Nordic (SWEDISH/NORWEGiAN/FiNNiSH), East-Asian (KOREAN/JAPANESE/CHINESE)
# and MULTi are absent from the defaults on purpose. Matching is whole-token,
# case-insensitive, within the scene-tag region after the year/episode marker, so
# a language word in the TITLE (e.g. 'Russian.Doll', 'The.Danish.Girl') is never
# falsely rejected.
_DEFAULT_DUB_TAGS = [
    "GERMAN", "FRENCH", "ITALIAN", "SPANISH", "POLISH", "RUSSIAN", "CZECH",
    "HUNGARIAN", "PORTUGUESE", "BRAZILIAN", "TURKISH", "DUTCH", "UKRAINIAN",
    "ROMANIAN", "BULGARIAN", "HINDI", "DANSK",
    # Scene language abbreviations (whole-token only, so they don't match inside
    # group names like SPARKS/RUSTED/PLUTONIUM). PLDUB/PLSUB = Polish dub/sub.
    "PL", "PLDUB", "PLSUB", "GER", "ITA", "SPA", "FRE", "RUS", "CZ", "HUN", "RO", "UA", "HEB",
]
# Subtitle stems: each matches '<stem>sub'/'<stem>subs' with an optional dot, e.g.
# DK -> DKsubs / DK.SUBS, as in 'Deep.Water.2026.Custom.DKsubs.1080p.WEB-DL...'.
_DEFAULT_SUB_TAGS = ["DK", "DANiSH"]
# Adult/porn scene tags (whole-token), e.g. 'Roccos.World...XXX'.
_DEFAULT_ADULT_TAGS = ["XXX"]

_NEVER_RE = re.compile(r"(?!)")  # matches nothing — used when a denylist is empty


def compile_dub_re(tags: list[str]) -> re.Pattern[str]:
    """Whole-token, case-insensitive alternation of foreign-dub tags (empty -> never)."""
    if not tags:
        return _NEVER_RE
    return re.compile(r"\b(?:" + "|".join(re.escape(t) for t in tags) + r")\b", re.I)


def compile_subs_re(stems: list[str]) -> re.Pattern[str]:
    """Match '<stem>sub'/'<stem>subs' (optional dot) for each stem (empty -> never)."""
    if not stems:
        return _NEVER_RE
    return re.compile(r"\b(?:" + "|".join(re.escape(s) for s in stems) + r")\.?SUBS?\b", re.I)


_FOREIGN_LANG_RE = compile_dub_re(_DEFAULT_DUB_TAGS)
_FOREIGN_SUBS_RE = compile_subs_re(_DEFAULT_SUB_TAGS)
_ADULT_TAGS = list(_DEFAULT_ADULT_TAGS)        # raw tags, for the title-exemption check
_ADULT_RE = compile_dub_re(_DEFAULT_ADULT_TAGS)  # whole-token match in the release name


def configure_filters(
    reject_dub_tags: list[str],
    reject_sub_tags: list[str],
    reject_adult_tags: list[str],
) -> None:
    """Recompile the dub/subtitle/adult denylists from config. Called once at
    startup; when the `filters` block is omitted the module defaults stay in effect."""
    global _FOREIGN_LANG_RE, _FOREIGN_SUBS_RE, _ADULT_TAGS, _ADULT_RE
    _FOREIGN_LANG_RE = compile_dub_re(reject_dub_tags)
    _FOREIGN_SUBS_RE = compile_subs_re(reject_sub_tags)
    _ADULT_TAGS = list(reject_adult_tags)
    _ADULT_RE = compile_dub_re(reject_adult_tags)


def release_has_required_tag(release_name: str, required_tags: list[str]) -> bool:
    """True if `release_name` carries at least one of `required_tags` in its
    scene-tag region (match.require_release_tags) — same whole-token,
    case-insensitive matching as the reject_* denylists, just inverted into an
    allowlist. Permissive (True) when required_tags is empty/unset, so this is
    a no-op for every item except the ones explicitly configured."""
    if not required_tags:
        return True
    return compile_dub_re(required_tags).search(_scene_tag_region(release_name)) is not None


def _scene_tag_region(name: str) -> str:
    """The scene-tag block AFTER the last year or SxxExx marker (falls back to the
    whole name when neither is present), so a language/sub word that is part of the
    TITLE isn't matched as a tag (e.g. 'Russian.Doll.S01E01...')."""
    end = 0
    for rx in (_YEAR_RE, _SEASON_OR_EP_RE):
        last = None
        for last in rx.finditer(name):
            pass  # keep only the last occurrence
        if last:
            end = max(end, last.end())
    return name[end:] if end else name


def is_foreign_language(name: str) -> bool:
    """True if `name` carries a rejected foreign-language dub tag (POLISH,
    GERMAN, FRENCH, …; from filters.reject_dub_tags). Scans only the scene tag block
    AFTER the year or SxxExx marker, so a language word that is part of the TITLE is
    not falsely rejected (e.g. 'Russian.Doll.S01E01...', 'The.French.Dispatch.2021...')."""
    return _FOREIGN_LANG_RE.search(_scene_tag_region(name)) is not None


def has_unwanted_subs(name: str) -> bool:
    """True if `name` carries an unwanted foreign-subtitle tag (DKsubs, …; stems from
    filters.reject_sub_tags). Scans only the scene-tag block after the year/episode
    marker, like is_foreign_language."""
    return _FOREIGN_SUBS_RE.search(_scene_tag_region(name)) is not None


def has_rejected_extension(files: list[dict], extensions: list[str]) -> bool:
    """True if any file in `files` (raw hub result dicts for one release group)
    ends with one of `extensions` (filters.reject_extensions), e.g. a DVDR
    release shipped as a single .img/.iso disc image instead of playable video
    files. Only sees files a hub actually listed individually — a release
    reported ONLY as an opaque whole-folder result can't be checked this way."""
    if not extensions:
        return False
    exts = tuple(f".{e.lstrip('.').lower()}" for e in extensions)
    for f in files:
        path = (f.get("path") or f.get("name") or "").lower()
        if path.endswith(exts):
            return True
    return False


_TITLE_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_LEADING_ARTICLES = ("the", "a", "an")


def _title_tokens(s: str) -> list[str]:
    """Lowercase alphanumeric tokens of a title's core name.

    Drops a leading article and any parenthetical qualifier — Sonarr appends
    region/year disambiguators like "The Office (US)" or "Bosch (2014)" that scene
    releases often omit, so requiring them would reject valid episodes.
    """
    s = to_ascii(s).lower().replace("'", "").replace("’", "").replace("‘", "")
    s = re.sub(r"\([^)]*\)", " ", s)  # strip "(US)", "(2019)", etc.
    toks = [t for t in _TITLE_SPLIT_RE.split(s) if t]
    if len(toks) > 1 and toks[0] in _LEADING_ARTICLES:
        toks = toks[1:]
    return toks


def _tokens_equal_loose(a: str, b: str, loose_trailing_s: bool = True) -> bool:
    """Token equality tolerant of a single trailing 's' on either side — Swedish
    (and some other languages') compound nouns link with an inserted '-s-'
    ("fotboll" -> "fotbolls-EM"), so a title's word and a scene release's
    corresponding word legitimately differ by exactly that letter. Still exact
    for everything else, so this doesn't turn into general fuzzy matching.
    `loose_trailing_s=False` (match.loose_trailing_s) makes it plain equality."""
    if a == b:
        return True
    if not loose_trailing_s:
        return False
    return a == b + "s" or b == a + "s"


def release_matches_title(release_name: str, title: str, anchored: bool = False) -> bool:
    """True if the requested title matches `release_name` as a token phrase.

    Guards against loose DC-hub matches grabbing a *different* show that merely
    shares a word — e.g. a 'Bad Judge' search returning 'Judge.Judy.S18E81', or
    'Star City' returning 'Star.Trek.Picard.S01E05.Stardust.City.Rag'. A plain
    token-overlap test passes both of those (they contain the words separately);
    requiring the title words *adjacent* (separated only by scene separators)
    rejects them while accepting 'Star.City.S01E01' and 'Bad.Judge.S01E01'.

    `anchored=True` additionally requires the title to LEAD the release name
    (optionally after an article) — scene naming is 'Series.Title.SxxExx...', so
    this rejects a different show whose EPISODE title contains the series name
    mid-release (e.g. 'DCs.Legends.of.Tomorrow.S01E06.Star.City.2046' for the
    series 'Star City'). Permissive when the title has no usable tokens.
    """
    want = _title_tokens(title)
    if not want:
        return True
    sep = r"[^a-z0-9]+"
    phrase = sep.join(re.escape(t) for t in want)
    if anchored:
        pattern = r"^(?:(?:the|a|an)[^a-z0-9]+)?" + phrase + r"(?![a-z0-9])"
    else:
        # Word-boundaried on alphanumerics so 'star' won't match inside 'stardust'.
        pattern = r"(?<![a-z0-9])" + phrase + r"(?![a-z0-9])"
    return re.search(pattern, release_name.lower()) is not None


def tv_release_extra_words_ok(
    release_name: str, title: str, year: Optional[int],
    tolerance: int = 1, loose_trailing_s: bool = True,
) -> bool:
    """Reject a same-titled DIFFERENT series wearing this series' name.

    release_matches_title(anchored=True) requires the series title to LEAD the
    release name, but puts no constraint on what sits between the title and the
    SxxExx marker — which let a 'Wallander' (2005) search grab the OLDER film
    adaptations packaged as episodes: 'Wallander.Hundarna.I.Riga.S01E01...',
    'Wallander.Villospar.2001.S01E03...'. Scene naming puts the episode title
    AFTER the marker ('Series.SxxExx.Episode.Title...'), so words wedged
    between the series title and the marker belong to a different work.

    Allowed between title and marker: nothing, or the series' own year
    disambiguator within ±tolerance ('Wallander.2005.S01E01' for a 2005
    series). A year outside tolerance ('Wallander.Villospar.2001.S01E03' for
    the 2005 series) or any other word is rejected. A season-subtitle year that
    IS part of the franchise name (e.g. 'American.Horror.Story.1984.S09E01')
    still matches via its alternate title, whose tokens include the year.
    Permissive when the release has no SxxExx token or the title is empty."""
    want = _title_tokens(title)
    if not want:
        return True
    rel = [t for t in _TITLE_SPLIT_RE.split(to_ascii(release_name).lower()) if t]
    if len(rel) > 1 and rel[0] in _LEADING_ARTICLES:
        rel = rel[1:]
    mi = next((i for i, t in enumerate(rel) if _EPISODE_RE.fullmatch(t)), None)
    if mi is None:
        return True
    n = 0
    for wt, rt in zip(want, rel):
        if not _tokens_equal_loose(wt, rt, loose_trailing_s):
            break
        n += 1
    for t in rel[n:mi]:
        if _YEAR_RE.fullmatch(t):
            if year and abs(int(t) - int(year)) > tolerance:
                return False
            continue
        return False
    return True


def release_starts_with_title(release_name: str, title: str, loose_trailing_s: bool = True) -> bool:
    """True if the release name BEGINS with the movie title, allowing the scene
    name to abbreviate a long title (e.g. 'Johan.Falk.GSI...' for 'Johan Falk:
    GSI - Gruppen...'). A 1-2 word title must appear in full at the start; a
    longer title needs at least its first 2 words. This rejects a release that
    merely CONTAINS the title mid-name (e.g. 'Roccos.World.Feet.Obsession.2.XXX'
    for the movie 'Obsession'). With `loose_trailing_s` (match.loose_trailing_s,
    default on) word comparison tolerates a single trailing 's' (see
    _tokens_equal_loose) for Swedish-style compound linking."""
    want = _title_tokens(title)
    if not want:
        return True
    rel = [t for t in _TITLE_SPLIT_RE.split(to_ascii(release_name).lower()) if t]
    if len(rel) > 1 and rel[0] in _LEADING_ARTICLES:
        rel = rel[1:]
    n = 0
    for wt, rt in zip(want, rel):
        if not _tokens_equal_loose(wt, rt, loose_trailing_s):
            break
        n += 1
    return n >= min(len(want), 2) and (len(want) > 2 or n == len(want))


def movie_title_prefix_ok(release_name: str, title: str, loose_trailing_s: bool = True) -> bool:
    """Stronger movie guard: the release's title portion — the tokens BEFORE its
    first year token — must be a prefix of the requested title's tokens, not merely
    share its opening words.

    release_starts_with_title() deliberately tolerates trailing words (to accept a
    scene name that abbreviates a long subtitle before the year, e.g.
    'Johan.Falk.GSI.2015' for 'Johan Falk: GSI - Gruppen...'). But that also lets a
    *different, longer-titled* film through when it merely begins with the same
    words — e.g. 'The.Odyssey.with.Dan.Snow.2026' matched the movie 'The Odyssey'.
    Requiring the pre-year tokens to be a prefix of the title rejects that (the
    title has no 'with dan snow') while still accepting the abbreviation case
    ('johan falk gsi' IS a prefix of the full title) and the exact match
    ('The.Odyssey.2026'). Yearless releases can't be split this way and pass
    through — the caller's release_starts_with_title() still applies to them.
    With `loose_trailing_s` (match.loose_trailing_s, default on) word comparison
    tolerates a single trailing 's' (see _tokens_equal_loose)."""
    want = _title_tokens(title)
    if not want:
        return True
    rel = [t for t in _TITLE_SPLIT_RE.split(to_ascii(release_name).lower()) if t]
    if len(rel) > 1 and rel[0] in _LEADING_ARTICLES:
        rel = rel[1:]
    yi = next((i for i, t in enumerate(rel) if _YEAR_RE.fullmatch(t)), None)
    if yi is None or yi == 0:
        # No year to split on, or the title itself starts with a year-like token
        # (e.g. '2001.A.Space.Odyssey'): can't strengthen, leave to starts_with.
        return True
    pre = rel[:yi]
    wanted_prefix = want[: len(pre)]
    return len(pre) == len(wanted_prefix) and all(
        _tokens_equal_loose(wt, rt, loose_trailing_s) for wt, rt in zip(wanted_prefix, pre)
    )


def is_adult_release(name: str, title: str) -> bool:
    """Reject scene-tagged adult content (tags from filters.reject_adult_tags,
    default XXX) for a non-adult request. A request whose own title carries an
    adult tag (e.g. 'xXx') is exempt so its own releases still match."""
    tl = title.lower()
    if any(t.lower() in tl for t in _ADULT_TAGS):
        return False
    return _ADULT_RE.search(name) is not None


# Air dates from Sonarr arrive as e.g. "2026-06-01T01:00:00Z". This sentinel is
# used for a wanted episode Sonarr has no air date for (TBA): treat it as already
# available (gate open, normal cadence) without ever forcing an immediate search.
_EPOCH_ISO = "1970-01-01T00:00:00Z"


def _utc_iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_to_epoch(s: str | None) -> int:
    """Parse a Sonarr airDateUtc to epoch seconds; 0 on missing/unparseable."""
    if not s:
        return 0
    try:
        return int(
            datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        )
    except (ValueError, TypeError):
        return 0


def _fmt_dur(secs: int) -> str:
    """Compact duration: '5d 4h', '3h 12m', '8m', 'now'."""
    secs = int(secs)
    if secs <= 0:
        return "now"
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _content_ref_epoch(item: dict) -> int:
    """Epoch the item's content became available, for content-age back-off:
    a movie's release date (real Radarr date, else its year as Jan 1), or a TV
    series' newest still-wanted aired episode. 0 when unknown (the caller then
    falls back to request-created age)."""
    if item.get("kind") == "tv":
        anchor = item.get("air_anchor_utc")
        return _iso_to_epoch(anchor) if anchor else 0
    rd = item.get("release_date_utc")
    if rd:
        return _iso_to_epoch(rd)
    year = item.get("year")
    return _iso_to_epoch(f"{int(year)}-01-01T00:00:00Z") if year else 0


def compute_cadence(item: dict, cfg: "Config", now_ts: int) -> dict:
    """Pure scheduling decision for one item — single source of truth shared by
    poll_item (does it search this sweep?) and the schedule report (when next?).

    Mirrors, in order: the availability gate (TV air date / movie release date),
    then the first-search-immediate rule, then the content-age back-off. Returns
    {due, status, next_due, detail}. `next_due` is the epoch the item next becomes
    eligible (None when unknown/complete). Does NOT cover the movie "already on
    disk" fast-skip (that's an async completed-table lookup, handled separately).
    """
    kind = item["kind"]
    last = int(item.get("last_searched_at") or 0)
    # A fresh request (never searched, or a deliberate re-request newer than the
    # last search) always gets ONE immediate probe search — even before the
    # release/air date. Early hub availability and wrong metadata dates happen;
    # after the probe stamps last_searched_at, the normal gate/back-off resumes.
    rc = int(item.get("request_created_at") or 0)
    initial = last == 0 or (rc and last < rc)
    _initial_due = {"due": True, "status": "initial", "next_due": now_ts,
                    "detail": "first search on fresh request"}
    # Backlog drain: episodes a previous poll left unsearched because of the
    # per-poll cap. Keep the item due EVERY sweep until the backlog clears, so a
    # large (often old) series isn't throttled to the content-age back-off with
    # episodes still un-searched. Only ever set for TV after a real search, so the
    # availability gate below is moot here — jump straight to due.
    if int(item.get("search_backlog") or 0) > 0:
        return {"due": True, "status": "draining", "next_due": now_ts,
                "detail": f"{int(item['search_backlog'])} episode(s) left to search"}
    # Per-kind availability gate: don't search before the content exists. TV works
    # off Sonarr air dates (the air_offset is already baked into next_air/air_anchor
    # at sync); movies off the release date + match.movie_release_offset_days.
    movie_ref = 0
    if kind == "tv":
        air_anchor = item.get("air_anchor_utc")
        next_air = item.get("next_air_utc")
        if not air_anchor:
            if not next_air:
                # Nothing wanted and nothing upcoming. A fresh request still gets
                # its one probe: a series tracked seconds ago by the add-webhook
                # has no air/episode data until the next *arr sync, and without
                # the probe it sits here misclassified as complete — the
                # react-immediately path silently no-ops and the item waits out
                # a full sync + sweep (poll_item's initial live-fetch pulls the
                # episode list straight from Sonarr instead). Beyond that one
                # probe: never poke a complete series (force-available flow also
                # keys on this status; poll_item stamps the probe as spent even
                # when it finds nothing, so this can't loop).
                if initial:
                    return _initial_due
                return {"due": False, "status": "complete", "next_due": None,
                        "detail": "no episode wanted"}
            if next_air > _utc_iso(now_ts):
                if initial:
                    return _initial_due
                return {"due": False, "status": "gated", "next_due": _iso_to_epoch(next_air),
                        "detail": f"airs+offset {next_air}"}
            # next_air already passed (just aired) -> fall through to back-off
        elif last < _iso_to_epoch(air_anchor):
            return {"due": True, "status": "aired", "next_due": now_ts,
                    "detail": f"episode aired {air_anchor}"}
    elif kind == "movie":
        rel = _content_ref_epoch(item)
        if rel:
            movie_ref = rel + int(cfg.match.movie_release_offset_days * 86400)
            if movie_ref > now_ts:
                if initial:
                    return _initial_due
                return {"due": False, "status": "gated", "next_due": movie_ref,
                        "detail": f"releases {_utc_iso(movie_ref)}"}
    # First search is always immediate: a fresh request (see `initial` above) gets
    # one search regardless of content-age back-off, then settles into the cadence.
    if initial:
        return {"due": True, "status": "due", "next_due": now_ts, "detail": "first search"}
    # Content-age back-off gap (None = no applicable tier -> search every sweep).
    # Age is measured from when the CONTENT became available (a movie's release date
    # + offset / a series' newest still-wanted aired episode), not when the request
    # was created — so a freshly-requested old title backs off immediately, while a
    # brand-new release keeps searching every sweep. Falls back to request age only
    # when no content date is known.
    gap = None
    ref = movie_ref or _content_ref_epoch(item) or int(item.get("request_created_at") or 0)
    if ref and cfg.poller.backoff:
        age = now_ts - ref
        applicable = [t for t in cfg.poller.backoff if age >= t.older_than_days * 86400]
        if applicable:
            gap = max(applicable, key=lambda t: t.older_than_days).search_every_seconds

    # Fresh-episode override: a TV episode that aired within fresh_episode_hours is
    # searched at (at most) the fresh cadence — capping, never slowing, the
    # back-off. gap is None for a brand-new request (no applicable tier), so it
    # keeps searching every sweep and newly-added items are not held back.
    fresh = False
    fresh_every = int(cfg.poller.fresh_episode_every_seconds)
    air_anchor = item.get("air_anchor_utc")
    if (kind == "tv" and air_anchor and fresh_every
            and now_ts - _iso_to_epoch(air_anchor) < cfg.poller.fresh_episode_hours * 3600):
        fresh = True
        if gap is not None:
            gap = min(gap, fresh_every)

    if gap is None:
        return {"due": True, "status": "due", "next_due": now_ts, "detail": "no back-off"}
    detail = f"fresh, every {_fmt_dur(gap)}" if fresh else f"every {_fmt_dur(gap)}"
    if now_ts - last < gap:
        return {"due": False, "status": "backoff", "next_due": last + gap, "detail": detail}
    return {"due": True, "status": "due", "next_due": now_ts, "detail": detail}


def score_result(
    name: str, size_bytes: int, quality: QualityCfg,
    priority: Optional[list[str]] = None,
) -> int:
    """Higher is better. Ranks by quality PREFERENCE — the per-item *arr profile
    order (priority) if available, else the config `priority`, else the
    `resolutions` order — then larger size as a tiebreak within the same tier."""
    name_l = name.lower()
    mb = int(size_bytes // (1024 * 1024))
    pri = priority or quality.priority
    if pri:
        rank = _priority_rank(name_l, pri)
        rank = len(pri) if rank is None else rank
        return (len(pri) - rank) * 1_000_000 + mb
    res = quality.resolutions
    rank = next((i for i, r in enumerate(res) if r.lower() in name_l), len(res))
    return (len(res) - rank) * 1_000_000 + mb


