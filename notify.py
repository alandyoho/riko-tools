#!/usr/bin/env python3
"""
notify.py — where the monitor sends alerts.

Two backends: ntfy (a push topic your phone subscribes to; zero-account, just pick
an unguessable topic) and a local log. Configure via [notify] in riko.toml or env.

The monitor distinguishes two kinds of message:
  * action_needed(...) — something a human must handle (pump keeps failing, food out,
    device offline). High priority.
  * fyi(...)           — something handled automatically (stall auto-cleared, clock
    re-synced) or a routine summary. Low priority.

This keeps "I fixed it" from crying wolf like "you need to act".
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("riko.notify")


@dataclass
class NotifyConfig:
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"

    @classmethod
    def from_sources(cls, cfg_obj=None) -> "NotifyConfig":
        topic = os.environ.get("RIKO_NTFY_TOPIC")
        server = os.environ.get("RIKO_NTFY_SERVER")
        # allow [notify] in the toml via the Config object if present
        raw = getattr(cfg_obj, "_notify", None) if cfg_obj else None
        if isinstance(raw, dict):
            topic = topic or raw.get("ntfy_topic")
            server = server or raw.get("ntfy_server")
        return cls(ntfy_topic=topic, ntfy_server=server or "https://ntfy.sh")


class Notifier:
    def __init__(self, config: NotifyConfig) -> None:
        self.config = config

    def _ntfy(self, title: str, body: str, priority: str, tags: str) -> None:
        if not self.config.ntfy_topic:
            return
        url = f"{self.config.ntfy_server.rstrip('/')}/{self.config.ntfy_topic}"
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), method="POST",
            headers={"Title": title, "Priority": priority, "Tags": tags},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:  # never let a failed notification break the monitor
            log.warning("ntfy send failed: %s", exc)

    def action_needed(self, title: str, body: str) -> None:
        log.warning("ACTION NEEDED: %s — %s", title, body)
        self._ntfy(f"⚠️ {title}", body, priority="high", tags="warning")

    def fyi(self, title: str, body: str) -> None:
        log.info("FYI: %s — %s", title, body)
        self._ntfy(title, body, priority="low", tags="information_source")
