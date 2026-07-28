"""Configuration models + loader for dc-bridge."""
from __future__ import annotations

import yaml
from pydantic import BaseModel, Field


class BridgeCfg(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"


class AirDCPPCfg(BaseModel):
    url: str
    username: str
    password: str
    hub_urls: list[str] = Field(default_factory=list)


class ArrCfg(BaseModel):
    url: str
    api_key: str = ""


class JellyseerrCfg(BaseModel):
    url: str = ""
    api_key: str = ""
    active_statuses: list[str] = Field(default_factory=lambda: ["pending", "approved", "processing"])
    force_available_on_stuck: bool = True         # force Jellyseerr -> available when *arr won't import
    force_available_grace_hours: float = 6.0      # wait this long after the grab lands before giving up on *arr


class PathMap(BaseModel):
    linux_root: str
    windows_root: str


class PathTranslate(BaseModel):
    """Prefix substitution from an *arr-side path to a host filesystem path.
    Multiple rules are tried in order; the first matching prefix wins.
    Example: arr_prefix "/share" -> fs_prefix "/mnt/user/media/verified" turns the
    sonarr root folder "/share/TV/Example.Show" into the actual host path
    "/mnt/user/media/verified/TV/Example.Show".
    """
    arr_prefix: str
    fs_prefix: str


class QualityCfg(BaseModel):
    # Name of the Sonarr/Radarr quality profile (same name in both apps, e.g.
    # "DC Single") that decides which qualities to grab and in what order. The
    # bridge looks it up by name and uses its allowed qualities (most-preferred
    # first) for ALL items, so you swap quality by editing one value. Empty =
    # use whatever profile each item is individually assigned in *arr.
    profile_name: str = ""
    # Ordered source+resolution preference, e.g. ["WEB 720p", "WEB 1080p", ...].
    # A release is accepted iff it matches at least one entry (all of the entry's
    # space-separated tokens present, case-insensitive substrings) and the
    # EARLIEST matching entry is the most preferred. Supersedes the legacy
    # accepted_keywords/resolutions pair below when set.
    priority: list[str] = []
    accepted_keywords: list[str] = []  # legacy: accepted source keywords (unordered)
    resolutions: list[str] = []        # legacy: accepted resolutions, in preference order
    episode_size_mb: tuple[int, int]
    movie_size_mb: tuple[int, int]


class BackoffTier(BaseModel):
    older_than_days: int                # requests older than this enter the tier
    search_every_seconds: int           # minimum gap between searches once in tier


class PollerCfg(BaseModel):
    interval_seconds: int = 900
    per_item_jitter_seconds: int = 60
    air_offset_hours: float = 0  # wait this long after a TV episode airs before searching
    download_grace_hours: float = 24  # how long a queued grab may take before it's deemed stalled
    fresh_episode_hours: float = 48          # TV: an episode aired within this window
    fresh_episode_every_seconds: int = 7200  # is searched at most this gap apart (caps back-off)
    # TV search pacing. Each still-needed episode is searched in its OWN AirDC++
    # instance (searches into a shared instance clobber each other — only the last
    # survives). AirDC++ also throttles rapid searches, so consecutive episode
    # searches are spaced out. settle = wait for results after firing a search;
    # gap = extra pause before the next episode's search; max_per_poll bounds how
    # many episodes one poll searches (the rest are picked up next sweep).
    tv_search_settle_seconds: float = 15.0
    tv_search_gap_seconds: float = 15.0
    tv_max_search_per_poll: int = 8
    # Stalled-grab fallback (TV episodes). A queued bundle that has downloaded 0
    # bytes and is older than stall_grace_minutes is treated as dead-sourced
    # ("File not available" / gone). The bridge removes it, remembers the release
    # so it's not re-grabbed, and re-searches — picking a different release and,
    # once a resolution's releases are exhausted, the next resolution in the
    # quality preference order. It gives up after max_stall_retries removals for
    # one episode (0 disables the whole fallback).
    stall_grace_minutes: float = 30.0
    max_stall_retries: int = 4
    # When the canonical title's hub search returns 0 results, retry with up to
    # this many of TMDB's alternate titles (regional/translated names a scene
    # release sometimes follows instead) before conceding the sweep. 0 disables.
    alt_title_search_limit: int = 3
    backoff: list[BackoffTier] = Field(default_factory=list)
    # When False (default), an episode/movie that was once fulfilled is NOT
    # re-downloaded if its file is later deleted — the completion marker keeps it
    # out of the search set. Set True for *arr-style behaviour: always re-grab a
    # monitored item whose file is missing. A deliberate Jellyseerr re-request
    # always re-fetches regardless of this setting.
    refetch_deleted: bool = False


class AutoSyncCfg(BaseModel):
    interval_seconds: int = 900  # 0 disables


class LoggingCfg(BaseModel):
    """File logging, on top of stdout (docker logs). Empty log_file = stdout only.
    The file resets on each start and rotates by size."""
    log_file: str = "/config/dc-bridge.log"
    max_size_mb: int = 50


class AutoApproveCfg(BaseModel):
    """Auto-approve PENDING Jellyseerr requests so they flow into *arr and get
    downloaded without manual approval. Movies are always approved; a TV request
    is approved only when its requested seasons total <= tv_max_episodes (season 0
    specials excluded), so a huge series isn't grabbed automatically. Disabled by
    default. Requires jellyseerr.url/api_key."""
    enabled: bool = False
    tv_max_episodes: int = 10


class ChildrenRoutingCfg(BaseModel):
    """Route children's content to dedicated *arr root folders by genre. When a
    movie/series carries any of `genres`, the bridge relocates its Radarr/Sonarr
    entry to the matching root (metadata move, moveFiles=false) so the download —
    and *arr's own management — lands there. Empty roots/genres disable it."""
    genres: list[str] = Field(default_factory=list)  # e.g. ["Family"]; empty disables
    movies_root: str = ""   # Radarr root, e.g. /share/Kids/Movies
    series_root: str = ""   # Sonarr root, e.g. /share/Kids/Series


class FiltersCfg(BaseModel):
    """Release denylists. A scene tag NOT listed is kept, so Nordic/East-Asian/MULTi
    tags are absent from the defaults on purpose. An empty list disables that filter."""
    # Foreign-DUB scene tags to reject (whole-token, case-insensitive).
    reject_dub_tags: list[str] = Field(default_factory=lambda: [
        "GERMAN", "FRENCH", "ITALIAN", "SPANISH", "POLISH", "RUSSIAN", "CZECH",
        "HUNGARIAN", "PORTUGUESE", "BRAZILIAN", "TURKISH", "DUTCH", "UKRAINIAN",
        "ROMANIAN", "BULGARIAN", "HINDI", "DANSK",
        "PL", "PLDUB", "PLSUB", "GER", "ITA", "SPA", "FRE", "RUS", "CZ", "HUN", "RO", "UA", "HEB",
    ])
    # Subtitle-language stems to reject on English-audio releases; each matches
    # "<stem>sub"/"<stem>subs" with an optional dot (DK -> DKsubs, DK.SUBS).
    reject_sub_tags: list[str] = Field(default_factory=lambda: ["DK", "DANiSH"])
    # Adult/porn scene tags to reject (whole-token). A request whose own title
    # carries one of these tags is exempt so its releases still match.
    reject_adult_tags: list[str] = Field(default_factory=lambda: ["XXX"])


class MatchCfg(BaseModel):
    """Match-correctness tuning. Most matching guards (movie title-at-start,
    anchored TV title, movie-vs-TV reject) are deliberately NOT configurable —
    they encode correctness, not taste, and loosening them re-opens wrong-grab
    bugs. The knobs here have a legitimate spread of user preference."""
    grab_specials: bool = False   # True = also grab Season 0 (S00) specials/OVAs
    # A movie's year, or (when tv_year_guard is on) a TV release's year if it
    # carries one, must be within ±this of the request/episode's broadcast year.
    year_tolerance: int = 1
    # Reject a TV release that carries a YEAR not within year_tolerance of the
    # specific episode's broadcast year — catches a same-titled remake produced
    # in a different year (e.g. a 2008 English "Wallander" release grabbed for
    # a 2005-aired episode of the Swedish original). A release with NO year is
    # never rejected by this (standard SxxExx naming legitimately omits it);
    # only a present, wrong year is a signal. Complements, not replaces,
    # require_release_tags — plenty of wrong-remake releases carry no year at
    # all and still need a tag override. False disables the guard entirely.
    tv_year_guard: bool = True
    # Title-word comparison tolerates a single trailing 's' between a title word
    # and the release's word ("fotboll" vs "fotbolls-EM") — Swedish-style compound
    # linking a scene release may render either way. False = exact words only.
    loose_trailing_s: bool = True
    # Don't search a movie until this many DAYS after its release date (digital,
    # else physical/cinema) — the movie equivalent of poller.air_offset_hours.
    # Scene WEB releases land around the digital date; bump this up if they tend to
    # appear later. 0 = search from the release date. Fractions allowed (0.5 = 12h).
    movie_release_offset_days: float = 0
    # Per-item required release tag(s), keyed by the bridge's item id (e.g.
    # "sonarr:845", as printed in its log lines). A release for that item must
    # carry at least one of the listed tags (same whole-token, case-insensitive
    # matching as filters.reject_dub_tags) or it's rejected. For a title that
    # collides with a differently-produced show of the same name (e.g. a
    # foreign original vs. an English-language remake, both plainly titled
    # "Wallander") where *arr's own language metadata can't be trusted to tell
    # them apart — Sonarr reported originalLanguage=English for the Swedish
    # 2005 series, so automatic detection isn't an option here.
    require_release_tags: dict[str, list[str]] = Field(default_factory=dict)


class Config(BaseModel):
    bridge: BridgeCfg = BridgeCfg()
    airdcpp: AirDCPPCfg
    sonarr: ArrCfg
    radarr: ArrCfg
    path_map: PathMap
    path_translate: list[PathTranslate] = Field(default_factory=list)
    quality: QualityCfg
    poller: PollerCfg = PollerCfg()
    auto_sync: AutoSyncCfg = AutoSyncCfg()
    jellyseerr: JellyseerrCfg = JellyseerrCfg()
    children_routing: ChildrenRoutingCfg = ChildrenRoutingCfg()
    logging: LoggingCfg = LoggingCfg()
    auto_approve: AutoApproveCfg = AutoApproveCfg()
    filters: FiltersCfg = FiltersCfg()
    match: MatchCfg = MatchCfg()


def load_config(path: str = "/config/config.yaml") -> Config:
    with open(path) as f:
        return Config.model_validate(yaml.safe_load(f))
