#!/usr/bin/env python3
"""dc-bridge entrypoint."""
from __future__ import annotations
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
log = logging.getLogger("dc_bridge")
from dcbridge.config import load_config
from dcbridge.helpers import configure_filters
from dcbridge.web import make_app
import uvicorn


def setup_logging(level: str, log_file: str = "", max_size_mb: int = 50) -> None:
    """Log to stdout (docker logs) and, when `log_file` is set, also to a rotating
    file. The file is RESET on each start (mode='w') and ROTATES by size once it
    reaches max_size_mb (one .1 backup kept). If the file can't be opened (e.g. the dir isn't mounted),
    we keep stdout-only instead of crashing."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]  # stdout
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            # Reset on startup: RotatingFileHandler forces mode='a' whenever
            # maxBytes>0, so truncate explicitly first to get a fresh log per start.
            open(log_file, "w").close()
            handlers.append(
                RotatingFileHandler(
                    log_file,
                    maxBytes=max(1, int(max_size_mb)) * 1024 * 1024,
                    backupCount=1,
                    encoding="utf-8",
                )
            )
        except OSError as e:
            print(f"dc-bridge: cannot open log file {log_file!r} ({e}); logging to stdout only")
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=handlers,
    )
    # httpx logs one INFO line per API call ("HTTP Request: GET ... 200 OK"),
    # which floods the log — the bridge makes hundreds of *arr calls per sync.
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ── Config models ────────────────────────────────────────────────────────────


# Config models + loader live in dcbridge/config.py


# ── Path translation ─────────────────────────────────────────────────────────


def main() -> None:
    cfg = load_config(os.environ.get("CONFIG_PATH", "/config/config.yaml"))
    configure_filters(cfg.filters.reject_dub_tags, cfg.filters.reject_sub_tags)
    setup_logging(cfg.bridge.log_level, cfg.logging.log_file, cfg.logging.max_size_mb)
    app = make_app(cfg)
    uvicorn.run(app, host=cfg.bridge.host, port=cfg.bridge.port, log_config=None)


if __name__ == "__main__":
    main()
