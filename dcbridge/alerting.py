"""Periodic check: dc-bridge's own staleness canary (see state.py's
last_synced_at) plus rargate's stuck-release count, written to a file a
host-side script reads to fire an unRAID notification.

Why a file instead of calling unRAID's notify script directly: that script is
a host-side PHP tool tied to /boot/config (agent settings) and
/tmp/notifications (the queue) via the full webGui include tree — bind-mounting
all of that into this container just to send a notification is far more host
coupling than the alert itself is worth. Writing a small JSON snapshot to a
path this container already has (or gets) write access to, and letting a tiny
host-side script/cron (deploy/alert-notify.sh) read it and call notify
natively, keeps the container's blast radius unchanged. See AlertingCfg's
docstring in config.py for the "method" dispatch this is one implementation of.

Dedup/cooldown is deliberately the READER's job (compare this snapshot against
what it last notified) — this side always writes the truthful current state,
never tries to remember what it already alerted about.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("dc_bridge")

from dcbridge.config import Config
from dcbridge.state import State


def _read_rargate_stuck_count(status_file: str) -> Optional[int]:
    """stuck_releases_count from rargate's status JSON. None (not 0) when the
    file is missing/unreadable/not configured — "unknown", never "all clear",
    so a rargate outage can't silently mask a real problem."""
    if not status_file:
        return None
    try:
        data = json.loads(Path(status_file).read_text())
        return int(data.get("stuck_releases_count", 0))
    except Exception as e:
        log.debug("alerting: could not read rargate status file %s: %s", status_file, e)
        return None


def build_alert_state(
    stale_tracking_count: int, rargate_stuck_count: Optional[int], now_ts: int,
) -> dict:
    """Pure: the current problem snapshot written to alert_file. Kept separate
    from the async check loop below so it's directly unit-testable without
    mocking state.db or the filesystem."""
    issues = []
    if stale_tracking_count > 0:
        issues.append({
            "id": "dc_bridge_stale_tracking",
            "severity": "warning",
            "subject": "dc-bridge: stale tracking data",
            "description": (
                f"{stale_tracking_count} active item(s) haven't been refreshed "
                f"by a sync pass in over 2x the auto-sync interval — a request "
                f"for one may silently go nowhere. Check GET /metrics."
            ),
        })
    if rargate_stuck_count:
        issues.append({
            "id": "rargate_stuck_releases",
            "severity": "warning",
            "subject": "rargate: stuck release(s)",
            "description": (
                f"{rargate_stuck_count} release(s) failing SFV validation "
                f"persistently — likely deleted/incomplete files. Check "
                f"rargate-status.json."
            ),
        })
    return {"checked_at": now_ts, "issues": issues}


async def alert_loop(app) -> None:
    cfg: Config = app.state.cfg
    ac = cfg.alerting
    if not ac.enabled:
        return
    if ac.method != "file":
        log.warning("alerting: method %r not implemented — loop not starting", ac.method)
        return
    state: State = app.state.state
    log.info(
        "alerting: checking every %ss, writing state to %s",
        ac.check_interval_seconds, ac.alert_file,
    )
    while True:
        try:
            now_ts = int(time.time())
            active_statuses = (
                set(cfg.jellyseerr.active_statuses)
                if cfg.jellyseerr.url and cfg.jellyseerr.api_key
                else None
            )
            stale_threshold_ts = now_ts - max(2 * cfg.auto_sync.interval_seconds, 1800)
            stale = await state.stale_active_items(stale_threshold_ts, active_statuses)
            rargate_stuck = _read_rargate_stuck_count(ac.rargate_status_file)
            snapshot = build_alert_state(len(stale), rargate_stuck, now_ts)
            Path(ac.alert_file).write_text(json.dumps(snapshot, indent=2))
        except Exception:
            log.exception("alerting: check failed")
        await asyncio.sleep(ac.check_interval_seconds)
