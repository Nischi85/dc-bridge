"""SQLite state store: tracked items + completion markers."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Optional

log = logging.getLogger("dc_bridge")


# ── State (SQLite) ───────────────────────────────────────────────────────────


SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked_items (
    id              TEXT PRIMARY KEY,    -- "sonarr:<series_id>" / "radarr:<movie_id>"
    kind            TEXT NOT NULL,       -- "tv" | "movie"
    title           TEXT NOT NULL,       -- canonical title
    target_dir_fs   TEXT NOT NULL,       -- e.g. /mnt/user/media/verified/<...>/<Title>/
    monitored_keys  TEXT,                -- JSON list (TV: ["S01","S03"]; movies: ["movie"])
    added_at        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS completed (
    item_id         TEXT NOT NULL,
    key             TEXT NOT NULL,       -- "S03E04" or "movie"
    bundle_id       TEXT,
    release_name    TEXT,
    queued_at       INTEGER NOT NULL,
    PRIMARY KEY(item_id, key)
);
"""


class State:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        # Idempotent column migrations. Tracked_items started life with just the
        # original schema; each feature added its column independently here.
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(tracked_items)").fetchall()}
        for name, ddl in [
            ("request_status",      "ALTER TABLE tracked_items ADD COLUMN request_status TEXT"),
            ("request_created_at",  "ALTER TABLE tracked_items ADD COLUMN request_created_at INTEGER"),
            ("last_searched_at",    "ALTER TABLE tracked_items ADD COLUMN last_searched_at INTEGER"),
            ("year",                "ALTER TABLE tracked_items ADD COLUMN year INTEGER"),
            ("air_anchor_utc",      "ALTER TABLE tracked_items ADD COLUMN air_anchor_utc TEXT"),
            ("next_air_utc",        "ALTER TABLE tracked_items ADD COLUMN next_air_utc TEXT"),
            ("jellyseerr_media_id", "ALTER TABLE tracked_items ADD COLUMN jellyseerr_media_id INTEGER"),
            ("quality_priority",    "ALTER TABLE tracked_items ADD COLUMN quality_priority TEXT"),
        ]:
            if name not in cols:
                self.conn.execute(ddl)
        self.conn.commit()
        self._lock = asyncio.Lock()

    async def add_item(
        self,
        id_: str,
        kind: str,
        title: str,
        target_dir_fs: str,
        monitored_keys: list[str] | None,
        year: int | None = None,
    ) -> None:
        async with self._lock:
            # UPSERT: on an existing row, update only the caller-provided columns
            # and leave everything else (request_status, last_searched_at,
            # air_anchor_utc, next_air_utc, jellyseerr_media_id, quality_priority,
            # request_created_at, added_at) untouched. year is COALESCEd so a
            # resync returning year=None keeps a previously-stored year. added_at
            # is only set on first insert, so it stays the true first-seen time.
            self.conn.execute(
                "INSERT INTO tracked_items"
                " (id, kind, title, target_dir_fs, monitored_keys, added_at, year)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "  kind=excluded.kind,"
                "  title=excluded.title,"
                "  target_dir_fs=excluded.target_dir_fs,"
                "  monitored_keys=excluded.monitored_keys,"
                "  year=COALESCE(excluded.year, tracked_items.year)",
                (id_, kind, title, target_dir_fs, json.dumps(monitored_keys or []),
                 int(time.time()), year),
            )
            self.conn.commit()

    async def remove_item(self, id_: str) -> None:
        async with self._lock:
            self.conn.execute("DELETE FROM tracked_items WHERE id = ?", (id_,))
            self.conn.execute("DELETE FROM completed WHERE item_id = ?", (id_,))
            self.conn.commit()

    async def list_items(self) -> list[dict]:
        async with self._lock:
            cur = self.conn.execute(
                "SELECT id, kind, title, target_dir_fs, monitored_keys, request_status,"
                " request_created_at, last_searched_at, year, air_anchor_utc, next_air_utc,"
                " jellyseerr_media_id, quality_priority FROM tracked_items"
            )
            return [
                {
                    "id": r[0],
                    "kind": r[1],
                    "title": r[2],
                    "target_dir_fs": r[3],
                    "monitored_keys": json.loads(r[4] or "[]"),
                    "request_status": r[5],
                    "request_created_at": r[6],
                    "last_searched_at": r[7],
                    "year": r[8],
                    "air_anchor_utc": r[9],
                    "next_air_utc": r[10],
                    "jellyseerr_media_id": r[11],
                    "quality_priority": json.loads(r[12]) if r[12] else [],
                }
                for r in cur.fetchall()
            ]

    async def set_request_created_at(self, item_id: str, ts: int) -> None:
        async with self._lock:
            self.conn.execute(
                "UPDATE tracked_items SET request_created_at = ? WHERE id = ?",
                (ts, item_id),
            )
            self.conn.commit()

    async def set_last_searched_at(self, item_id: str, ts: int) -> None:
        async with self._lock:
            self.conn.execute(
                "UPDATE tracked_items SET last_searched_at = ? WHERE id = ?",
                (ts, item_id),
            )
            self.conn.commit()

    async def set_tv_air(
        self, item_id: str, air_anchor: str | None, next_air: str | None
    ) -> None:
        """Stamp a TV series' air-date gate fields (computed each sonarr sync):
        air_anchor_utc = newest aired-and-wanted episode; next_air_utc = soonest
        still-to-air wanted episode."""
        async with self._lock:
            self.conn.execute(
                "UPDATE tracked_items SET air_anchor_utc = ?, next_air_utc = ? WHERE id = ?",
                (air_anchor, next_air, item_id),
            )
            self.conn.commit()

    async def clear_all_request_statuses(self) -> None:
        """Set every tracked item's request_status to NULL. Called at the start
        of a Jellyseerr sync so items that transitioned out of active state
        (e.g. became 'available') are correctly demoted, not left stale.
        """
        async with self._lock:
            self.conn.execute("UPDATE tracked_items SET request_status = NULL")
            self.conn.commit()

    async def set_request_status(self, item_id: str, status: str) -> bool:
        """Mark a single tracked item with a Jellyseerr request status. Returns
        True if the item existed (was matched).
        """
        async with self._lock:
            cur = self.conn.execute(
                "UPDATE tracked_items SET request_status = ? WHERE id = ?",
                (status, item_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    async def list_completed(self, item_id: str) -> list[dict]:
        async with self._lock:
            cur = self.conn.execute(
                "SELECT key, bundle_id, release_name, queued_at FROM completed WHERE item_id = ?",
                (item_id,),
            )
            return [
                {"key": r[0], "bundle_id": r[1], "release_name": r[2], "queued_at": r[3]}
                for r in cur.fetchall()
            ]

    async def is_completed(self, item_id: str, key: str) -> bool:
        async with self._lock:
            cur = self.conn.execute(
                "SELECT 1 FROM completed WHERE item_id = ? AND key = ?", (item_id, key)
            )
            return cur.fetchone() is not None

    async def mark_completed(
        self, item_id: str, key: str, bundle_id: Optional[str], release_name: Optional[str]
    ) -> None:
        async with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO completed (item_id, key, bundle_id, release_name, queued_at)"
                " VALUES (?,?,?,?,?)",
                (item_id, key, bundle_id, release_name, int(time.time())),
            )
            self.conn.commit()

    async def get_completed(self, item_id: str, key: str) -> Optional[tuple[Optional[str], int]]:
        """Return (release_name, queued_at) for a completed marker, or None."""
        async with self._lock:
            cur = self.conn.execute(
                "SELECT release_name, queued_at FROM completed WHERE item_id = ? AND key = ?",
                (item_id, key),
            )
            row = cur.fetchone()
            return (row[0], int(row[1] or 0)) if row else None

    async def clear_completed(self, item_id: str, key: str) -> None:
        async with self._lock:
            self.conn.execute(
                "DELETE FROM completed WHERE item_id = ? AND key = ?", (item_id, key)
            )
            self.conn.commit()

    async def get_completed_keys(self, item_id: str) -> list[tuple[str, Optional[str], int]]:
        """All completed markers for an item: [(key, release_name, queued_at), ...]."""
        async with self._lock:
            cur = self.conn.execute(
                "SELECT key, release_name, queued_at FROM completed WHERE item_id = ?",
                (item_id,),
            )
            return [(r[0], r[1], int(r[2] or 0)) for r in cur.fetchall()]

    async def set_quality_priority(self, item_id: str, priority: list[str]) -> None:
        """Store the item's *arr-quality-profile-derived preference order (ordered
        '<source> <resolution>' specs, most-preferred first)."""
        async with self._lock:
            self.conn.execute(
                "UPDATE tracked_items SET quality_priority = ? WHERE id = ?",
                (json.dumps(priority or []), item_id),
            )
            self.conn.commit()

    async def set_monitored_keys(self, item_id: str, keys: list[str]) -> None:
        """Store a TV series' still-wanted aired episode keys (monitored, no file
        in Sonarr, already aired) so the poller knows the full search set."""
        async with self._lock:
            self.conn.execute(
                "UPDATE tracked_items SET monitored_keys = ? WHERE id = ?",
                (json.dumps(keys or []), item_id),
            )
            self.conn.commit()

    async def set_jellyseerr_media_id(self, item_id: str, media_id: Optional[int]) -> None:
        async with self._lock:
            self.conn.execute(
                "UPDATE tracked_items SET jellyseerr_media_id = ? WHERE id = ?",
                (media_id, item_id),
            )
            self.conn.commit()


