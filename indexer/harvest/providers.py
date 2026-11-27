# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Provider registry TOML-driven."""

from __future__ import annotations

import json
import random
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

RE_JSONP = re.compile(r"^[a-zA-Z0-9_$.]+\((.*)\)\s*;?\s*$", re.DOTALL)


class ProviderError(Exception):
    pass


class ProviderBlocked(ProviderError):
    pass


class ProviderParseError(ProviderError):
    pass


@dataclass
class Provider:
    name: str
    spec: dict[str, Any]
    http_cfg: dict[str, Any]
    ua_pool: list[str]

    def build_url(self, q: str, hl: str, gl: str) -> str:
        kl = "id-id"
        if "kl_map" in self.spec:
            kl = self.spec["kl_map"].get(
                f"{hl}-{gl}", self.spec.get("kl_fallback", "id-id")
            )
        return self.spec["url"].format(q=urllib.parse.quote(q), hl=hl, gl=gl, kl=kl)

    def headers(self, hl: str, gl: str) -> dict[str, str]:
        return {
            "User-Agent": random.choice(self.ua_pool),
            "Accept": self.http_cfg["accept"],
            "Accept-Language": self.http_cfg["accept_lang_fmt"].format(
                hl=hl, gl_upper=gl.upper()
            ),
            "Referer": self.spec["referer"],
        }

    def parse(self, raw: str) -> list[str]:
        raw = raw.strip()
        m = RE_JSONP.match(raw)
        if m:
            raw = m.group(1)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProviderParseError(f"{self.name}: {e}") from e

        kind = self.spec["kind"]
        if kind == "json_array_pair":
            if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list):
                return [s for s in data[1] if isinstance(s, str)]
            return []
        if kind == "json_array_of_objects":
            kp = self.spec["key_path"]
            return [
                it[kp]
                for it in data
                if isinstance(it, dict) and isinstance(it.get(kp), str)
            ]
        if kind == "json_object_r":
            kp = self.spec["key_path"]
            return [
                it[kp]
                for it in data.get("r", [])
                if isinstance(it, dict) and isinstance(it.get(kp), str)
            ]
        raise ProviderParseError(f"{self.name}: unknown kind '{kind}'")


def load_providers(cfg) -> dict[str, Provider]:
    http_cfg = cfg.http
    ua_pool = cfg.ua_pool
    return {
        name: Provider(name, spec, http_cfg, ua_pool)
        for name, spec in cfg.providers.items()
    }
