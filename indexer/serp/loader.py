# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helper untuk membaca blok [serp_filter] dari IndexerConfig."""

from __future__ import annotations

from typing import Any


def get_serp_filter_config(cfg: Any, category: str) -> dict[str, Any]:
    """Ambil konfigurasi filter SERP untuk kategori tertentu."""
    rule = getattr(cfg, "categories", {}).get(category)
    if not rule:
        return {}
    # Asumsi: config_loader sudah menyimpan raw TOML di rule.raw atau rule.toml
    return getattr(rule, "raw", {}).get("serp_filter", {})


def get_all_brands(cfg: Any) -> list[dict]:
    """Ambil daftar brand dari semua kategori."""
    brands: list[dict] = []
    for cat, rule in getattr(cfg, "categories", {}).items():
        raw = getattr(rule, "raw", {})
        for b in raw.get("queries", {}).get("brands", []):
            brands.append({"key": b, "names": [b], "domains": [], "ambiguous": False})
    return brands
