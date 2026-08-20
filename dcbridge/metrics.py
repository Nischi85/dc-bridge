"""In-process counters for /metrics — process-lifetime only (reset on restart),
same convention as a Prometheus counter. Persistent, cross-restart stats (grabs
per day, staleness) are derived straight from state.db instead — see web.py's
/metrics route. This module exists only for the numbers nothing in the DB can
answer: how many search rounds/queues/errors happened since this process started.
"""
from __future__ import annotations
import time


class Counters:
    def __init__(self) -> None:
        self.started_at = int(time.time())
        self.searches_run = 0
        self.items_queued = 0
        self.poll_errors = 0
        self.sync_errors = 0

    def as_dict(self, now_ts: int) -> dict:
        return {
            "uptime_seconds": now_ts - self.started_at,
            "searches_run": self.searches_run,
            "items_queued": self.items_queued,
            "poll_errors": self.poll_errors,
            "sync_errors": self.sync_errors,
        }


# Single process-wide instance — the same pattern as util.py's module-level
# pooled http client, for the same reason (one bridge process per container).
counters = Counters()
