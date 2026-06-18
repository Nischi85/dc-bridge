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
    backoff: list[BackoffTier] = Field(default_factory=list)


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


def load_config(path: str = "/config/config.yaml") -> Config:
    with open(path) as f:
        return Config.model_validate(yaml.safe_load(f))
