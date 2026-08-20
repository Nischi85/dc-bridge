# dc-bridge

A small self-hosted service that connects **Jellyseerr → Sonarr/Radarr → AirDC++**,
so media requested in Jellyseerr is automatically searched on your DC++ hubs,
queued with its full set of companion files (`.rar` + `.sfv` + `.nfo` + sample),
and delivered into a tidy source layout
(`verified/TV/<Show>/Season.<N>/<release>/` or `verified/Movies/<release>/`).

It is the "downloader" half of a setup where a FUSE layer (e.g.
[rargate](https://github.com/Nischi85/RARgate)) later exposes the extracted media
to your library — but dc-bridge only handles searching and queuing; it does not
extract or serve files itself.

> **Heads up:** this is a personal homelab tool, shared as-is. It assumes you
> already run AirDC++, Sonarr, Radarr, and (optionally) Jellyseerr, and that
> AirDC++ writes into a folder your media stack can read.

## How it works

1. You add a movie/series in Radarr/Sonarr (directly, or via a Jellyseerr request).
2. Radarr/Sonarr fire a webhook at dc-bridge; it records the item and where its
   files should live.
3. On a schedule, dc-bridge searches your AirDC++ hubs for each active item,
   picks the best release for the item's quality profile, and queues the whole
   release folder for download. If the canonical title finds nothing, it retries
   with the title's TMDB alternate titles (`poller.alt_title_search_limit`) —
   scene releases sometimes follow a regional/translated title's wording.
4. As downloads land, dc-bridge nudges Radarr/Sonarr to import them, and
   (optionally) flips the Jellyseerr request to *available*.

State (tracked items, what's been grabbed, last-search times) lives in a small
SQLite database so restarts are cheap.

## What the bridge filters

For each item the bridge evaluates every candidate release folder and rejects the
ones that don't fit. Some of that is yours to tune in `config.yaml`; some is fixed
because it's about matching *correctly*, not taste.

**Configurable** (see `config.yaml.example`):

- **Quality + size** — `quality.profile_name` names the Sonarr/Radarr quality
  profile whose allowed qualities decide what to grab and in what order;
  `quality.episode_size_mb` / `movie_size_mb` bound the total release size.
- **Content filters** (`filters:`) — denylists for foreign-dub tags
  (`reject_dub_tags`), foreign-subtitle languages (`reject_sub_tags`), and
  adult/porn tags (`reject_adult_tags`). A tag you don't list is kept; an empty
  list disables that filter. Matching is whole-token within the scene-tag block
  after the year/episode marker, so a language word in a *title* (e.g.
  `The.Danish.Girl`) is never mistaken for a tag. `reject_extensions`
  (default `[img, iso]`) rejects a release if any file a hub lists for it ends
  in one of these — e.g. a DVDR shipped as a single disc image instead of
  playable video files. Only sees files a hub actually lists individually; a
  release reported only as an opaque whole-folder result can't be checked.
- **Match preferences** (`match:`) — `grab_specials` (include Season 0
  specials/OVAs, default off), `year_tolerance` (how far a movie's year, or a
  TV release's year if it carries one, may differ from the request/episode's
  broadcast year, default ±1), `movie_release_offset_days` (wait this many
  days after a movie's release date before searching), `loose_trailing_s`
  (tolerate a single trailing "s" between a title word and the release's word,
  e.g. Swedish-style compound linking; default on, off = exact words only),
  `tv_year_guard` (reject a TV release whose carried year doesn't match the
  specific episode's broadcast year — a yearless release is never rejected by
  this; default on), and `require_release_tags` (per-item allowlist, keyed by
  the bridge's item id, e.g. `"sonarr:845": ["SWEDiSH"]` — for a title that
  collides with a differently-produced same-named show that *arr's own
  metadata can't tell apart; empty by default, a no-op until you add an
  entry).

The bridge also won't search content before it exists: a TV episode is gated
until it airs (`poller.air_offset_hours`), and a movie until its release date
(`match.movie_release_offset_days`). Search frequency afterward is driven by how
old the content is, not when it was requested — see `poller.backoff`.

**Not configurable, on purpose** — these guards encode correctness, and loosening
them just re-opens wrong-grab bugs, so they live in code:

- A movie release must **start with** the movie title (rejects a different film
  that merely contains the title mid-name).
- A TV release must carry the series title as a **contiguous phrase** (the hub
  search is a loose token match, so "Bad Judge" otherwise pulls in "Judge Judy").
- Between the series title and the `SxxExx` marker a TV release may carry
  **nothing but the series' own year** (±`match.year_tolerance`) — words or a
  different year there mean a same-titled *different* series/adaptation wearing
  this series' name (scene naming puts episode titles after the marker).
- A movie request never accepts a release carrying a **season/episode** marker
  (that's a TV pack, not the film).

## Requirements

- Docker (the examples use an unRAID host, but any Docker host works).
- AirDC++ with its Web API enabled, and a download location reachable from the
  host as a normal path.
- Sonarr and Radarr (API keys).
- Jellyseerr is optional — without it, dc-bridge polls everything Sonarr/Radarr
  knows about; with it, the worklist is filtered to active requests.

## Files in this repo

| Path | What |
|---|---|
| `bridge.py` | Entrypoint (logging + app startup). |
| `dcbridge/` | The package: `config`, `state`, `helpers`, `util`, `airdcpp`, `arr`, `poller`, `web`. |
| `Dockerfile`, `requirements.txt` | Image build. |
| `config.yaml.example` | Annotated config template — copy it, fill it in. |
| `docker-compose.yml` | Standalone compose file if you don't use the unRAID GUI. |
| `dc-bridge.unraid-template.xml` | unRAID Docker template for the GUI. |

Your real config and runtime state are **not** in git (`config.yaml`, `state.db`,
and `*.log` are gitignored).

## Setup

1. **Configure.** Copy the template and fill in your URLs + API keys:
   ```
   cp config.yaml.example config.yaml
   # then edit config.yaml — AirDC++, Sonarr, Radarr, (Jellyseerr), and the
   # path_map / path_translate so dc-bridge knows where AirDC++ writes.
   ```
   At runtime the container reads its config from `/config/config.yaml`, so put
   `config.yaml` in whatever host directory you mount to `/config`.

2. **Build the image** (tag `latest` — the unRAID template references it):
   ```
   cd /path/to/dc-bridge
   docker build -t dc-bridge:latest .
   ```

3. **Run it.** Either install the unRAID template (below) and use the GUI, or
   run directly. dc-bridge listens on port `8000`; give it an address your
   Sonarr/Radarr can reach (the example uses a macvlan IP — adjust to your LAN):
   ```
   docker run -d --name dc-bridge \
     --network br0 --ip <bridge-ip> \
     -v /path/to/dc-bridge-config:/config \
     -e CONFIG_PATH=/config/config.yaml \
     --restart unless-stopped \
     dc-bridge:latest
   ```

4. **Wire the webhooks** in Sonarr and Radarr (Settings → Connect → Webhook):
   - URL: `http://<bridge-ip>:8000/webhook/sonarr` and `.../webhook/radarr`
   - Triggers: `On Series Add` / `On Series Delete` (Sonarr),
     `On Movie Added` / `On Movie Delete` (Radarr), plus `Test`.

5. **(Optional) Wire the Jellyseerr webhook** so newly-requested items are
   searched within seconds instead of on the next sweep. The *arr webhooks above
   only fire when a whole series/movie is *added*; a new episode of an
   already-tracked series fires nothing there, so Jellyseerr is the only
   immediate signal for it. In Jellyseerr **Settings → Notifications → Webhook**:
   - Webhook URL: `http://<bridge-ip>:8000/webhook/jellyseerr`
   - Notification Types: `Request Pending Approval`, `Request Approved`,
     `Request Automatically Approved`
   - JSON Payload: leave the default (it includes `notification_type`)
   - Enable the agent. On any request event the bridge approves it (if needed),
     re-syncs Jellyseerr's status, force-refreshes that item's Sonarr/Radarr
     data (superseding whatever the periodic ~15 min sync last cached), and
     immediately searches the freshly-requested item.

6. **Bootstrap existing items** so dc-bridge learns about anything added before
   the webhooks existed:
   ```
   curl -X POST http://<bridge-ip>:8000/sync
   ```

7. **(Optional) Enable fs_watch** if Sonarr/Radarr's library is a FUSE view
   (e.g. a rargate/rar2fs mount) that can't be deleted through directly. In
   that setup, deleting a bad/wrong-language release has to happen on the
   *real* backing storage — but *arr never sees that deletion, so its hasFile
   record stays stale forever and the bridge never re-tracks the item. With
   `fs_watch` enabled, the bridge watches the real storage itself and fires a
   targeted rescan the moment something under it is deleted, so *arr catches
   up and the item falls back into the normal search flow with no manual
   rescan step. Bind-mount the real path read-only and point `watch_root` at
   the exact same path in `config.yaml`:
   ```
   -v /mnt/zzd/share/fin:/mnt/zzd/share/fin:ro
   ```
   ```yaml
   fs_watch:
     enabled: true
     watch_root: /mnt/zzd/share/fin
   ```
   Not needed if *arr's library sits on storage it can delete from directly.

## unRAID GUI template

Copy `dc-bridge.unraid-template.xml` into
`/boot/config/plugins/dockerMan/templates-user/dc-bridge.xml`, then in the
unRAID **Docker** tab → **Add Container** → *User templates* → **dc-bridge** →
**Apply**. From then on the GUI **Edit** button manages the container (IP,
mounts, env vars). Set the IP in the form before applying.

The image is built locally (not on Docker Hub), so **don't use unRAID's "Force
update"** — it tries to pull from a registry. To ship code changes, rebuild the
image and recreate the container (see below); a plain **Edit → Apply** that
doesn't change the repository field won't re-pull.

## Day-to-day

- **Change config:** edit your `config.yaml`, then `docker restart dc-bridge`
  (config is re-read on startup; no rebuild needed).
- **Change code:** rebuild and recreate:
  ```
  docker build -t dc-bridge:latest .
  docker stop dc-bridge && docker rm dc-bridge
  # ...then the docker run command from Setup step 3
  ```

### HTTP endpoints

| Call | What it does |
|---|---|
| `POST /sync` | Re-learn items from Sonarr/Radarr/Jellyseerr (safe to re-run). |
| `POST /poll/radarr:<id>` (or `sonarr:<id>`) | Search + queue one item now. |
| `GET /state?only_active=true` | Show the active worklist. |
| `GET /metrics` | Operational snapshot: process counters (searches/queues/errors since start) + state.db-derived stats (grabs last 24h/7d, active item count, and `stale_tracking` — any active item whose tracking data hasn't actually been refreshed by a sync pass in over 2x the auto-sync interval, even if periodic syncs are otherwise succeeding). Check this first for "why didn't X download." |
| `GET /airdcpp/probe?q=<query>` | Dry-run a hub search (no download) to sanity-check results. |
| webhooks: `POST /webhook/{sonarr,radarr}` | Where Sonarr/Radarr notify the bridge (series/movie add → immediate search). |
| `POST /webhook/jellyseerr` | Where Jellyseerr notifies the bridge; approves + syncs + searches freshly-requested items now. |

Logs: `docker logs -f dc-bridge` (and a rotating file if `logging.log_file` is set).

### Tests

```
pip install -r requirements-dev.txt
pytest tests/ -v
```

Covers the pure matching/quality/scheduling logic in `helpers.py` (`tests/test_matching_guards.py`)
and the `compute_cadence` scheduling state machine (`tests/test_cadence.py`) — the parts most
prone to a silent wrong-grab or a silently-stuck item if a guard regresses. Not run as part of
the Docker build (kept out of the runtime image); run manually or wire into CI.

## Disaster recovery

Config and state live together in the mounted `/config` directory. Back that up,
or just re-run `POST /sync` after a rebuild — it reconstructs the tracked items
from Sonarr/Radarr and re-reads Jellyseerr status. The only thing lost in a full
wipe is the per-item last-search timestamps (back-off restarts at zero: one extra
catch-up sweep, then normal cadence).

## License

[MIT](LICENSE) © Nischi85
