#!/usr/bin/env python3
"""Reads dc-bridge's pending_alerts.json (written by dcbridge/alerting.py's
alert_loop, cfg.alerting.method="file"), diffs it against what was last
notified, and fires an unRAID notification for each newly-appeared or
newly-resolved issue.

Runs HOST-SIDE on a schedule via cron (see dc-bridge-alerts.cron), never
inside the dc-bridge container — unRAID's own notify script depends on
host-only state (/boot/config for agent settings, /tmp/notifications for the
queue) via its webGui include tree, and bind-mounting all of that into the
container just to send a notification would be far more host coupling than
the alert is worth. This script is the one thing that actually needs to run
on the host; the container only ever writes a plain JSON snapshot.

Dedup lives here, not in the container: pending_alerts.json is always the
CURRENT truthful state (may repeat every check interval); this script only
notifies when the issue set actually changes since last_notified_alerts.json.
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

ALERT_FILE = Path("/mnt/cache/dc-bridge/pending_alerts.json")
LAST_NOTIFIED_FILE = Path("/mnt/cache/dc-bridge/last_notified_alerts.json")
NOTIFY = "/usr/local/emhttp/webGui/scripts/notify"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _notify(subject: str, description: str, severity: str) -> None:
    subprocess.run(
        [NOTIFY, "-e", "dc-bridge", "-s", subject, "-d", description, "-i", severity],
        check=False,
    )


def main() -> int:
    current = _load_json(ALERT_FILE)
    if not current:
        return 0  # bridge hasn't written a snapshot yet (or alerting is off)

    current_issues = {i["id"]: i for i in current.get("issues", [])}
    last = _load_json(LAST_NOTIFIED_FILE)
    last_issues: dict = last.get("issues") or {}

    for new_id in set(current_issues) - set(last_issues):
        i = current_issues[new_id]
        _notify(i["subject"], i["description"], i.get("severity", "warning"))

    for resolved_id in set(last_issues) - set(current_issues):
        prev = last_issues[resolved_id]
        _notify(f"{prev['subject']} — resolved", "No longer detected.", "normal")

    LAST_NOTIFIED_FILE.write_text(json.dumps({"issues": current_issues}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
