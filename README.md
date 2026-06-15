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
   release folder for download.
4. As downloads land, dc-bridge nudges Radarr/Sonarr to import them, and
   (optionally) flips the Jellyseerr request to *available*.

State (tracked items, what's been grabbed, last-search times) lives in a small
SQLite database so restarts are cheap.

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
     re-syncs, and immediately searches the freshly-requested item.

6. **Bootstrap existing items** so dc-bridge learns about anything added before
   the webhooks existed:
   ```
   curl -X POST http://<bridge-ip>:8000/sync
   ```

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
| `GET /airdcpp/probe?q=<query>` | Dry-run a hub search (no download) to sanity-check results. |
| webhooks: `POST /webhook/{sonarr,radarr}` | Where Sonarr/Radarr notify the bridge (series/movie add → immediate search). |
| `POST /webhook/jellyseerr` | Where Jellyseerr notifies the bridge; approves + syncs + searches freshly-requested items now. |

Logs: `docker logs -f dc-bridge` (and a rotating file if `logging.log_file` is set).

## Disaster recovery

Config and state live together in the mounted `/config` directory. Back that up,
or just re-run `POST /sync` after a rebuild — it reconstructs the tracked items
from Sonarr/Radarr and re-reads Jellyseerr status. The only thing lost in a full
wipe is the per-item last-search timestamps (back-off restarts at zero: one extra
catch-up sweep, then normal cadence).

## License

[MIT](LICENSE) © Nischi85
