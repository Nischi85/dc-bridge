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

-- Releases that were queued for a key but stalled (dead sources / "File not
-- available") and got removed. Excluded from future candidate selection so the
-- retry picks a different release — and, once a quality's releases are exhausted,
-- the next resolution in the preference order.
CREATE TABLE IF NOT EXISTS failed_releases (
    item_id         TEXT NOT NULL,
    key             TEXT NOT NULL,       -- "S03E04" or "movie"
    release_name    TEXT NOT NULL,
    failed_at       INTEGER NOT NULL,
    PRIMARY KEY(item_id, key, release_name)
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
            ("release_date_utc",    "ALTER TABLE tracked_items ADD COLUMN release_date_utc TEXT"),
            ("search_backlog",      "ALTER TABLE tracked_items ADD COLUMN search_backlog INTEGER"),
            ("requested_seasons",   "ALTER TABLE tracked_items ADD COLUMN requested_seasons TEXT"),
            ("alt_titles",          "ALTER TABLE tracked_items ADD COLUMN alt_titles TEXT"),
            ("episode_air_years",   "ALTER TABLE tracked_items ADD COLUMN episode_air_years TEXT"),
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
            self.conn.execute("DELETE FROM failed_releases WHERE item_id = ?", (id_,))
            self.conn.commit()

    _ITEM_COLUMNS = (
        "id, kind, title, target_dir_fs, monitored_keys, request_status,"
        " request_created_at, last_searched_at, year, air_anchor_utc, next_air_utc,"
        " jellyseerr_media_id, quality_priority, release_date_utc, search_backlog,"
        " requested_seasons, alt_titles, episode_air_years"
    )

    @staticmethod
    def _row_to_item(r) -> dict:
        return {
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
            "release_date_utc": r[13],
            "search_backlog": r[14] or 0,
            "requested_seasons": json.loads(r[15]) if r[15] else None,
            "alt_titles": json.loads(r[16]) if r[16] else [],
            "episode_air_years": json.loads(r[17]) if r[17] else {},
        }

    async def list_items(self) -> list[dict]:
        async with self._lock:
            cur = self.conn.execute(f"SELECT {self._ITEM_COLUMNS} FROM tracked_items")
            return [self._row_to_item(r) for r in cur.fetchall()]

    async def get_item(self, item_id: str) -> Optional[dict]:
        """One tracked item by id, or None — avoids the full-table list_items
        scan (+ per-row JSON parsing) for single-item lookups."""
        async with self._lock:
            cur = self.conn.execute(
                f"SELECT {self._ITEM_COLUMNS} FROM tracked_items WHERE id = ?", (item_id,)
            )
            row = cur.fetchone()
            return self._row_to_item(row) if row else None

    def _update_if_changed(self, sql: str, params: tuple) -> None:
        """Run an UPDATE whose WHERE clause already excludes no-op writes (the
        caller appends `AND <col> IS NOT ?`), committing only when a row really
        changed. The sync loops re-stamp every tracked item every cycle with
        values that are almost always identical, and every commit is an fsync —
        without this, each 15-min sync burned thousands of no-op disk writes."""
        cur = self.conn.execute(sql, params)
        if cur.rowcount:
            self.conn.commit()

    async def set_request_created_at(self, item_id: str, ts: int) -> None:
        async with self._lock:
            self._update_if_changed(
                "UPDATE tracked_items SET request_created_at = ?"
                " WHERE id = ? AND request_created_at IS NOT ?",
                (ts, item_id, ts),
            )

    async def set_release_date(self, item_id: str, release_date_utc: str | None) -> None:
        """Stamp a movie's release date (ISO UTC) for content-age back-off."""
        async with self._lock:
            self._update_if_changed(
                "UPDATE tracked_items SET release_date_utc = ?"
                " WHERE id = ? AND release_date_utc IS NOT ?",
                (release_date_utc, item_id, release_date_utc),
            )

    async def set_last_searched_at(self, item_id: str, ts: int) -> None:
        # No changed-value guard: a search stamp is always a new timestamp.
        async with self._lock:
            self.conn.execute(
                "UPDATE tracked_items SET last_searched_at = ? WHERE id = ?",
                (ts, item_id),
            )
            self.conn.commit()

    async def set_search_backlog(self, item_id: str, n: int) -> None:
        """How many still-needed episodes were left unsearched this poll because of
        the per-poll cap. >0 keeps the item due every sweep (compute_cadence) until
        the backlog drains, instead of dropping into the content-age back-off."""
        async with self._lock:
            self._update_if_changed(
                "UPDATE tracked_items SET search_backlog = ?"
                " WHERE id = ? AND search_backlog IS NOT ?",
                (int(n), item_id, int(n)),
            )

    async def add_failed_release(self, item_id: str, key: str, release_name: str) -> None:
        async with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO failed_releases (item_id, key, release_name, failed_at)"
                " VALUES (?,?,?,?)",
                (item_id, key, release_name, int(time.time())),
            )
            self.conn.commit()

    async def get_failed_releases(self, item_id: str, key: str) -> set[str]:
        async with self._lock:
            cur = self.conn.execute(
                "SELECT release_name FROM failed_releases WHERE item_id = ? AND key = ?",
                (item_id, key),
            )
            return {r[0] for r in cur.fetchall()}

    async def set_tv_air(
        self, item_id: str, air_anchor: str | None, next_air: str | None
    ) -> None:
        """Stamp a TV series' air-date gate fields (computed each sonarr sync):
        air_anchor_utc = newest aired-and-wanted episode; next_air_utc = soonest
        still-to-air wanted episode."""
        async with self._lock:
            self._update_if_changed(
                "UPDATE tracked_items SET air_anchor_utc = ?, next_air_utc = ?"
                " WHERE id = ? AND (air_anchor_utc IS NOT ? OR next_air_utc IS NOT ?)",
                (air_anchor, next_air, item_id, air_anchor, next_air),
            )

    async def clear_request_statuses_except(self, keep: set[str]) -> None:
        """Demote items no longer active — NULL request_status for every item NOT
        in `keep`, in ONE statement. Used at the END of a Jellyseerr sync instead
        of a clear-at-start, so a concurrent poller sweep never sees the transient
        all-NULL window (which made it skip the whole sweep — '0 items to search')."""
        async with self._lock:
            if keep:
                placeholders = ",".join("?" * len(keep))
                self.conn.execute(
                    f"UPDATE tracked_items SET request_status = NULL"
                    f" WHERE request_status IS NOT NULL AND id NOT IN ({placeholders})",
                    tuple(keep),
                )
            else:
                self.conn.execute(
                    "UPDATE tracked_items SET request_status = NULL"
                    " WHERE request_status IS NOT NULL"
                )
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
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO completed (item_id, key, bundle_id, release_name, queued_at)"
                " VALUES (?,?,?,?,?)",
                (item_id, key, bundle_id, release_name, int(time.time())),
            )
            # Already marked (the common resync case — every *arr sync re-marks
            # every hasFile item): nothing changed, so skip the cleanup DELETE
            # and the commit entirely. Thousands of no-op fsyncs per sync
            # otherwise.
            if not cur.rowcount:
                return
            # A finished key needs no failed-release history anymore.
            self.conn.execute(
                "DELETE FROM failed_releases WHERE item_id = ? AND key = ?", (item_id, key)
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
            v = json.dumps(priority or [])
            self._update_if_changed(
                "UPDATE tracked_items SET quality_priority = ?"
                " WHERE id = ? AND quality_priority IS NOT ?",
                (v, item_id, v),
            )

    async def set_monitored_keys(self, item_id: str, keys: list[str]) -> None:
        """Store a TV series' still-wanted aired episode keys (monitored, no file
        in Sonarr, already aired) so the poller knows the full search set."""
        async with self._lock:
            v = json.dumps(keys or [])
            self._update_if_changed(
                "UPDATE tracked_items SET monitored_keys = ?"
                " WHERE id = ? AND monitored_keys IS NOT ?",
                (v, item_id, v),
            )

    async def set_requested_seasons(self, item_id: str, seasons: list[int]) -> None:
        """Store the season numbers actually covered by this TV item's active
        Jellyseerr request(s) (union, when there's more than one) — used to keep
        the poller from backfilling a season Sonarr happens to have monitored
        (e.g. leftover from before this item was ever actively tracked) but that
        was never actually requested."""
        async with self._lock:
            v = json.dumps(sorted(seasons)) if seasons else None
            self._update_if_changed(
                "UPDATE tracked_items SET requested_seasons = ?"
                " WHERE id = ? AND requested_seasons IS NOT ?",
                (v, item_id, v),
            )

    async def set_alt_titles(self, item_id: str, titles: list[str]) -> None:
        """Store TMDB's alternate titles for this item (from Radarr's/Sonarr's
        alternateTitles), tried as fallback search queries when the canonical
        title's hub search comes back empty — scene releases sometimes follow a
        regional/translated title's wording instead of the canonical one."""
        async with self._lock:
            v = json.dumps(titles or [])
            self._update_if_changed(
                "UPDATE tracked_items SET alt_titles = ?"
                " WHERE id = ? AND alt_titles IS NOT ?",
                (v, item_id, v),
            )

    async def set_episode_air_years(self, item_id: str, years: dict[str, int]) -> None:
        """Store each wanted episode's air year ({"S01E01": 2005, ...}), computed
        at Sonarr sync from its airDateUtc. Used by the TV year guard
        (match.tv_year_guard) to reject a release whose year doesn't match the
        SPECIFIC episode it claims to be — a per-item show year isn't precise
        enough for a long-running series spanning many broadcast years."""
        async with self._lock:
            v = json.dumps(years or {})
            self._update_if_changed(
                "UPDATE tracked_items SET episode_air_years = ?"
                " WHERE id = ? AND episode_air_years IS NOT ?",
                (v, item_id, v),
            )

    async def set_jellyseerr_media_id(self, item_id: str, media_id: Optional[int]) -> None:
        async with self._lock:
            self._update_if_changed(
                "UPDATE tracked_items SET jellyseerr_media_id = ?"
                " WHERE id = ? AND jellyseerr_media_id IS NOT ?",
                (media_id, item_id, media_id),
            )


