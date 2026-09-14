"""Lightweight logging setup."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in getattr(record, "extra_data", {}).items():
            payload[k] = v
        return json.dumps(payload)


def setup_logging(level: str = "INFO", file_path: str | None = None, json_log: bool = False) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(level)
    # Remove existing handlers to avoid duplicates on reload
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = JsonFormatter() if json_log else logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        "%H:%M:%S",
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    if file_path:
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            file_path, maxBytes=5_000_000, backupCount=3
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # Quiet noisy libs
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    return logging.getLogger("bot")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
