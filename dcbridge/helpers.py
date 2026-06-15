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
    # Collapse whitespace.
    s = re.sub(r"\s+", " ", s).strip()
    return s


def episode_keys_from_name(name: str) -> list[str]:
    """Return ['S03E04', ...] for any episode markers in `name`."""
    return [f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}" for m in _EPISODE_RE.finditer(name)]


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
            # res==0 (e.g. plain "DVD") -> match the source alone, since such
            # releases (DVDRip/XviD) usually carry no resolution token.
            spec = f"{src} {res}p" if res else src
            if spec not in specs:
                specs.append(spec)
    return specs


async def _fetch_quality_profiles(url: str, headers: dict, http: httpx.AsyncClient) -> dict:
    """{profileId: ['web 720p', ...]} for a Sonarr/Radarr instance. Empty on error
    so the bridge falls back to the config quality rules."""
    try:
        r = await http.get(f"{url}/api/v3/qualityprofile", headers=headers)
        if r.status_code == 200:
            return {p["id"]: profile_to_priority(p) for p in r.json()}
    except Exception as e:
        log.debug("could not fetch quality profiles from %s: %s", url, e)
    return {}


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


_SD_SOURCE_RE = re.compile(r"\b(?:dvdrip|dvdscr|dvd-?r|dvd|xvid|divx|sdtv|vhsrip|tvrip)\b", re.I)


def is_sd_release(name: str) -> bool:
    """A DVD/SD-source release (DVDRip, XviD, …). These legitimately omit the
    year, so the yearless-junk year-guard is relaxed for them."""
    return bool(_SD_SOURCE_RE.search(name))


# Foreign-language dub tags to reject. Note the deliberate exclusions: Nordic
# (NORDiC/SWEDISH/DANiSH/NORWEGiAN/FiNNiSH) and East Asian (KOREAN/JAPANESE/
# CHINESE) tags are NOT rejected, and neither is MULTi/MULTiSUBS (multi-language,
# usually includes English). Edit this pattern to match the languages you keep.
_FOREIGN_LANG_RE = re.compile(
    r"\b(?:GERMAN|FRENCH|ITALIAN|SPANISH|POLISH|RUSSIAN|CZECH|HUNGARIAN|"
    r"PORTUGUESE|BRAZILIAN|TURKISH|DUTCH|UKRAINIAN|ROMANIAN|BULGARIAN|HINDI|"
    # Scene language abbreviations (whole-token only, so they don't match inside
    # group names like SPARKS/RUSTED/PLUTONIUM). PLDUB/PLSUB = Polish dub/sub.
    r"PL|PLDUB|PLSUB|GER|ITA|SPA|FRE|RUS|CZ|HUN|RO|UA|HEB)\b",
    re.I,
)


def is_foreign_language(name: str) -> bool:
    """True if `name` carries a rejected foreign-language dub tag (POLISH,
    GERMAN, FRENCH, …; see _FOREIGN_LANG_RE). Scans only the scene tag block AFTER the year or SxxExx
    marker, so a language word that is part of the TITLE is not falsely rejected
    (e.g. 'Russian.Doll.S01E01...', 'The.French.Dispatch.2021...'). Falls back to
    the whole name when neither marker is present (yearless SD release)."""
    end = 0
    for rx in (_YEAR_RE, _SEASON_OR_EP_RE):
        last = None
        for last in rx.finditer(name):
            pass  # keep only the last occurrence
        if last:
            end = max(end, last.end())
    region = name[end:] if end else name
    return _FOREIGN_LANG_RE.search(region) is not None


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


def release_starts_with_title(release_name: str, title: str) -> bool:
    """True if the release name BEGINS with the movie title, allowing the scene
    name to abbreviate a long title (e.g. 'Johan.Falk.GSI...' for 'Johan Falk:
    GSI - Gruppen...'). A 1-2 word title must appear in full at the start; a
    longer title needs at least its first 2 words. This rejects a release that
    merely CONTAINS the title mid-name (e.g. 'Roccos.World.Feet.Obsession.2.XXX'
    for the movie 'Obsession')."""
    want = _title_tokens(title)
    if not want:
        return True
    rel = [t for t in _TITLE_SPLIT_RE.split(to_ascii(release_name).lower()) if t]
    if len(rel) > 1 and rel[0] in _LEADING_ARTICLES:
        rel = rel[1:]
    n = 0
    for wt, rt in zip(want, rel):
        if wt != rt:
            break
        n += 1
    return n >= min(len(want), 2) and (len(want) > 2 or n == len(want))


def is_adult_release(name: str, title: str) -> bool:
    """Reject scene-tagged adult content (XXX) for a non-adult request — scene
    porn is reliably tagged 'XXX'. The rare adult-named title (e.g. 'xXx') is
    exempt so its own releases still match."""
    if "xxx" in title.lower():
        return False
    return re.search(r"\bxxx\b", name, re.I) is not None


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


def compute_cadence(item: dict, cfg: "Config", now_ts: int) -> dict:
    """Pure scheduling decision for one item — single source of truth shared by
    poll_item (does it search this sweep?) and the schedule report (when next?).

    Mirrors, in order: the TV air-date gate, then the age-based back-off. Returns
    {due, status, next_due, detail}. `next_due` is the epoch the item next becomes
    eligible (None when unknown/complete). Does NOT cover the movie "already on
    disk" fast-skip (that's an async completed-table lookup, handled separately).
    """
    kind = item["kind"]
    last = int(item.get("last_searched_at") or 0)
    if kind == "tv":
        air_anchor = item.get("air_anchor_utc")
        next_air = item.get("next_air_utc")
        if not air_anchor:
            if not next_air:
                return {"due": False, "status": "complete", "next_due": None,
                        "detail": "no episode wanted"}
            if next_air > _utc_iso(now_ts):
                return {"due": False, "status": "gated", "next_due": _iso_to_epoch(next_air),
                        "detail": f"airs+offset {next_air}"}
            # next_air already passed (just aired) -> fall through to back-off
        elif last < _iso_to_epoch(air_anchor):
            return {"due": True, "status": "aired", "next_due": now_ts,
                    "detail": f"episode aired {air_anchor}"}
    created_at = item.get("request_created_at")
    if created_at and cfg.poller.backoff:
        age = now_ts - int(created_at)
        applicable = [t for t in cfg.poller.backoff if age >= t.older_than_days * 86400]
        if applicable:
            tier = max(applicable, key=lambda t: t.older_than_days)
            gap = tier.search_every_seconds
            if now_ts - last < gap:
                return {"due": False, "status": "backoff", "next_due": last + gap,
                        "detail": f"every {_fmt_dur(gap)}"}
            return {"due": True, "status": "due", "next_due": now_ts,
                    "detail": f"every {_fmt_dur(gap)}"}
    return {"due": True, "status": "due", "next_due": now_ts, "detail": "no back-off"}


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


