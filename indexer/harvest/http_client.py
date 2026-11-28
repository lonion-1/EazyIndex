# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: MIT
"""HTTP client untuk suggest API (urllib, no deps)."""

from __future__ import annotations

import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .providers import Provider, ProviderBlocked, ProviderError


@dataclass
class HttpClient:
    http_cfg: dict[str, Any]

    def fetch(self, provider: Provider, q: str, hl: str, gl: str) -> list[str]:
        url = provider.build_url(q, hl, gl)
        req = urllib.request.Request(url, headers=provider.headers(hl, gl))
        timeout = self.http_cfg.get("timeout_sec", 8)
        max_bytes = self.http_cfg.get("max_bytes", 250_000)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                cs = resp.headers.get_content_charset() or "utf-8"
                raw = resp.read(max_bytes).decode(cs, errors="ignore")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise ProviderBlocked(f"HTTP {e.code}") from e
            raise ProviderError(f"HTTP {e.code}") from e
        return provider.parse(raw)

    def sleep_jitter(self) -> None:
        base = self.http_cfg.get("backoff_base_sec", 0.8)
        jitter = self.http_cfg.get("backoff_jitter_sec", 0.8)
        time.sleep(base + random.uniform(0, jitter))
