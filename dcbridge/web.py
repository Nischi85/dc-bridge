"""FastAPI app: webhook models, lifespan, routes."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Optional
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
log = logging.getLogger("dc_bridge")
from dcbridge.config import (
    Config,
)
from dcbridge.helpers import (
    _truncate,
    episode_keys_from_name,
    passes_quality,
)
from dcbridge.util import (
    _try_smb,
    http_session,
)
from dcbridge.state import (
    State,
)
from dcbridge.airdcpp import (
    AirDCPP,
)
from dcbridge.arr import (
    _sync_jellyseerr,
    _sync_radarr,
    _sync_sonarr,
    auto_approve_requests,
)
from dcbridge.poller import (
    auto_sync_loop,
    handle_radarr_event,
    handle_sonarr_event,
    poll_item,
    poller_loop,
    write_schedule_report,
)


class SonarrWebhook(BaseModel):
    eventType: str
    series: Optional[dict] = None


class RadarrWebhook(BaseModel):
    eventType: str
    movie: Optional[dict] = None


# ── App + lifecycle ──────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: Config = app.state.cfg
    state = State("/config/state.db")
    airdcpp = AirDCPP(cfg.airdcpp)
    try:
        await airdcpp.ensure_auth()
    except Exception:
        log.exception("airdcpp: initial auth failed; will retry on demand")
    app.state.state = state
    app.state.airdcpp = airdcpp

    poller_task = asyncio.create_task(poller_loop(app))
    auto_sync_task: Optional[asyncio.Task] = None
    if cfg.auto_sync.interval_seconds > 0:
        auto_sync_task = asyncio.create_task(auto_sync_loop(app))
    log.info(
        "dc-bridge ready: webhook on :%s, polling every %ss, auto-sync every %ss",
        cfg.bridge.port,
        cfg.poller.interval_seconds,
        cfg.auto_sync.interval_seconds,
    )
    try:
        yield
    finally:
        for t in (poller_task, auto_sync_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        await airdcpp.close()


def make_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="dc-bridge", version="1.0.0", lifespan=lifespan)
    app.state.cfg = cfg

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/state")
    async def state_dump(only_active: bool = False):
        state: State = app.state.state
        items = await state.list_items()
        active = set(cfg.jellyseerr.active_statuses)
        out = []
        for it in items:
            it["completed"] = await state.list_completed(it["id"])
            it["target_dir_smb"] = _try_smb(it["target_dir_fs"], cfg.path_map)
            if only_active and it.get("request_status") not in active:
                continue
            out.append(it)
        return {"items": out, "count": len(out)}

    @app.get("/airdcpp/probe")
    async def airdcpp_probe(q: str = "Example.Show.S01", wait: float = 8.0):
        """Drive a full search round-trip (create instance, hub_search, wait, get
        results) and return a compact preview. Does NOT queue downloads. Use for
        sanity-checking what AirDC++ returns for a given query/quality settings.
        """
        ad: AirDCPP = app.state.airdcpp
        await ad.ensure_auth()
        iid = await ad.create_search_instance()
        if iid is None:
            return {"error": "create_search_instance failed"}
        await ad.hub_search(iid, q, extensions=None)
        await asyncio.sleep(wait)
        results = await ad.get_results(iid, 0, 100)
        await ad.delete_instance(iid)
        preview = [
            {
                "name": r.get("name"),
                "tth": r.get("tth"),
                "size": r.get("size"),
                "type": (r.get("type") or {}).get("str"),
                "path": r.get("path"),
                "users": (r.get("users") or {}).get("count"),
                "episode_keys": episode_keys_from_name(r.get("name") or ""),
                "passes_quality_tv": passes_quality(
                    r.get("name") or "",
                    int(r.get("size") or 0),
                    "tv",
                    cfg.quality,
                ),
            }
            for r in results
        ]
        return {"instance": iid, "count": len(results), "results": preview}

    @app.post("/poll/{item_id:path}")
    async def poll_now(item_id: str):
        """Run a single poll cycle for one tracked item immediately.

        item_id is the bridge's composite id, e.g. "radarr:1234" or "sonarr:5".
        Useful for testing / one-off "go look for this now" without waiting for
        the next sweep. The fast-skip rule for completed movies still applies.
        """
        state: State = app.state.state
        items = await state.list_items()
        item = next((i for i in items if i["id"] == item_id), None)
        if not item:
            raise HTTPException(404, f"no tracked item {item_id}")
        ad: AirDCPP = app.state.airdcpp
        await poll_item(cfg, state, ad, item)
        return {"ok": True, "item_id": item_id, "title": item.get("title")}

    @app.post("/sync")
    async def sync_from_arr():
        """One-shot import of currently-monitored series/movies from *arr APIs.

        Use this to bootstrap the bridge with items that existed BEFORE the
        webhooks were wired (anything in Jellyseerr/your library prior to today).
        Idempotent: safe to run multiple times — INSERT OR REPLACE on tracked
        items, INSERT OR IGNORE on completed keys.

        Also marks already-downloaded items as completed so the poller does not
        try to re-grab them: radarr.hasFile=true -> completed key "movie";
        sonarr episodes with hasFile=true -> completed key "S03E04" etc.
        """
        state: State = app.state.state
        report: dict[str, Any] = {"sonarr": {}, "radarr": {}}
        async with http_session() as http:
            try:
                report["auto_approve"] = await auto_approve_requests(cfg, http)
            except Exception as e:
                log.exception("sync auto-approve failed")
                report["auto_approve"] = {"error": str(e)}
            try:
                report["sonarr"] = await _sync_sonarr(cfg, state, http)
            except Exception as e:
                log.exception("sync sonarr failed")
                report["sonarr"] = {"error": str(e)}
            try:
                report["radarr"] = await _sync_radarr(cfg, state, http)
            except Exception as e:
                log.exception("sync radarr failed")
                report["radarr"] = {"error": str(e)}
            # Jellyseerr filter runs LAST so it sees the freshly-(re)imported
            # tracked items and can stamp their request_status correctly.
            try:
                report["jellyseerr"] = await _sync_jellyseerr(cfg, state, http)
            except Exception as e:
                log.exception("sync jellyseerr failed")
                report["jellyseerr"] = {"error": str(e)}
        try:
            await write_schedule_report(state, cfg, app.state.airdcpp, int(time.time()))
        except Exception:
            log.exception("sync: schedule report failed")
        return report

    @app.get("/bundles")
    async def list_bundles_ep():
        """List AirDC++ queue bundles via the bridge's own session."""
        ad: AirDCPP = app.state.airdcpp
        bundles = await ad.list_bundles()
        if bundles is None:
            return {"error": "could not list bundles"}
        return [
            {"id": b.get("id"), "name": b.get("name"),
             "status": (b.get("status") or {}).get("str"),
             "completed": (b.get("status") or {}).get("completed")}
            for b in bundles
        ]

    @app.post("/bundles/{bundle_id}/remove")
    async def remove_bundle_ep(bundle_id: int, remove_finished: bool = False):
        ad: AirDCPP = app.state.airdcpp
        ok = await ad.remove_bundle(bundle_id, remove_finished=remove_finished)
        return {"ok": ok, "bundle_id": bundle_id}

    @app.post("/webhook/sonarr")
    async def webhook_sonarr(req: Request):
        body = await req.json()
        log.info("sonarr webhook: %s", _truncate(json.dumps(body), 600))
        try:
            ev = SonarrWebhook.model_validate(body)
        except Exception as e:
            raise HTTPException(400, f"bad sonarr payload: {e}")
        await handle_sonarr_event(app, ev)
        return {"ok": True}

    @app.post("/webhook/radarr")
    async def webhook_radarr(req: Request):
        body = await req.json()
        log.info("radarr webhook: %s", _truncate(json.dumps(body), 600))
        try:
            ev = RadarrWebhook.model_validate(body)
        except Exception as e:
            raise HTTPException(400, f"bad radarr payload: {e}")
        await handle_radarr_event(app, ev)
        return {"ok": True}

    return app


