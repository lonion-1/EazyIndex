# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loader untuk config/auto_suggest.toml."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CONFIG_PATH = ROOT / "config" / "auto_suggest.toml"

_FALLBACK_REL_PATHS: tuple[Path, ...] = (
    Path("config/auto_suggest.toml"),
    Path("_vault/config/auto_suggest.toml"),
    Path("_vault/_config/auto_suggest.toml"),
    Path("EazyVault/config/auto_suggest.toml"),
)


def _resolve_config_path(path: str | Path | None = None) -> Path:
    """Cari lokasi file auto_suggest.toml dari argumen, env, CWD, atau ROOT repo."""
    if path is not None:
        return Path(path)

    env_cfg = os.environ.get("EAZY_AUTO_SUGGEST_CFG", "").strip()
    if env_cfg:
        return Path(env_cfg).expanduser()

    for base in (Path.cwd(), ROOT):
        for rel in _FALLBACK_REL_PATHS:
            candidate = base / rel
            if candidate.is_file():
                return candidate

    return DEFAULT_CONFIG_PATH


@dataclass(frozen=True)
class HarvestConfig:
    raw: dict[str, Any] = field(default_factory=dict)

    # --- shortcuts ---
    @property
    def http(self) -> dict[str, Any]:
        return self.raw.get("HTTP", {})

    @property
    def ua_pool(self) -> list[str]:
        return self.http.get("UserAgents", {}).get("pool", [])

    @property
    def providers(self) -> dict[str, dict]:
        return {
            k: v
            for k, v in self.raw.get("Provider", {}).items()
            if isinstance(v, dict) and v.get("enabled", True)
        }

    @property
    def routing_default(self) -> dict[str, float]:
        default_blk = self.raw.get("Routing", {}).get("Default", {})
        if isinstance(default_blk, dict):
            if "providers" in default_blk and isinstance(
                default_blk["providers"], dict
            ):
                return default_blk["providers"]
            return default_blk
        return {}

    @property
    def routing_override(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for k, v in self.raw.get("Routing", {}).get("Override", {}).items():
            if isinstance(v, dict):
                if "providers" in v and isinstance(v["providers"], dict):
                    out[k] = v["providers"]
                else:
                    out[k] = v
        return out

    @property
    def locale(self) -> dict[str, Any]:
        return self.raw.get("Locale", {})

    @property
    def modifiers(self) -> dict[str, dict[str, list[str]]]:
        return self.raw.get("Modifier", {})

    @property
    def probe(self) -> dict[str, Any]:
        return self.raw.get("Probe", {})

    @property
    def harvest(self) -> dict[str, Any]:
        return self.raw.get("Harvest", {})

    @property
    def filter(self) -> dict[str, Any]:
        return self.raw.get("Filter", self.raw.get("filter", {}))

    @property
    def filters(self) -> dict[str, Any]:
        return self.filter

    def resolve_year(self) -> str:
        src = self.raw.get("Meta", {}).get("year_src", "utc")
        if isinstance(src, int):
            return str(src)
        if src == "local":
            return str(time.localtime().tm_year)
        return str(time.gmtime().tm_year)


def load_auto_suggest(path: str | Path | None = None) -> HarvestConfig:
    """Muat konfigurasi auto_suggest.toml dari path eksplisit atau fallback."""
    p = _resolve_config_path(path)
    with p.open("rb") as f:
        return HarvestConfig(raw=tomllib.load(f))
