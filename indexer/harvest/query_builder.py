# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build probe query & clean suggestion."""

from __future__ import annotations

import random
from typing import Any

try:
    from ..text_utils import norm_space
except ImportError:

    def norm_space(s: str) -> str:
        return " ".join(s.split())


def _resolve_modifier_pool(
    cat: str,
    hl: str | None,
    probe_cfg: dict[str, Any],
    modifiers: dict[str, Any],
) -> tuple[list[str], list[str] | None]:
    """Ambil pool modifier dari format baru (by_category/by_locale) maupun lama."""
    if not isinstance(modifiers, dict):
        return list(probe_cfg.get("default_modifiers", ["terbaru"])), None

    # Format lama: modifiers[cat] = {"prefix": [...], "suffix": [...]}
    legacy = modifiers.get(cat)
    if isinstance(legacy, dict) and ("prefix" in legacy or "suffix" in legacy):
        return list(legacy.get("prefix", [])), list(legacy.get("suffix", []))
    if isinstance(legacy, list):
        return list(legacy), None

    # Format baru: [Modifier.by_category] & [Modifier.by_locale]
    by_cat = modifiers.get("by_category", {})
    by_loc = modifiers.get("by_locale", {})

    cat_pool = list(by_cat.get(cat, [])) if isinstance(by_cat, dict) else []
    loc_pool = list(by_loc.get(hl, [])) if (hl and isinstance(by_loc, dict)) else []

    if loc_pool and cat_pool:
        pool = loc_pool if random.random() < 0.65 else cat_pool
    else:
        pool = (
            loc_pool
            or cat_pool
            or list(probe_cfg.get("default_modifiers", ["terbaru"]))
        )

    return pool, None


def build_probe_query(
    seed: str,
    cat: str,
    year: str,
    probe_cfg: dict[str, Any],
    modifiers: dict[str, Any],
    hl: str | None = None,
) -> str:
    r = random.random()
    bare_p = float(probe_cfg.get("bare_prob", 0.45))
    raw_year_p = float(probe_cfg.get("year_prob", 0.25))
    year_cutoff = raw_year_p if raw_year_p > bare_p else min(0.95, bare_p + raw_year_p)

    if r < bare_p:
        return seed
    if r < year_cutoff:
        return f"{seed} {year}"

    pool_a, pool_b = _resolve_modifier_pool(cat, hl, probe_cfg, modifiers)

    if pool_b is not None:
        use_prefix = random.random() < 0.5
        pool = pool_a if use_prefix else pool_b
        if not pool:
            return seed
        mod = random.choice(pool).replace("{year}", year)
        if not mod or mod.lower() in seed.lower():
            return seed
        return f"{mod} {seed}" if use_prefix else f"{seed} {mod}"

    if not pool_a:
        mod = probe_cfg.get("fallback_modifier", "terbaru")
        return f"{seed} {mod}".strip()

    mod = random.choice(pool_a).replace("{year}", year)
    if not mod or mod.lower() in seed.lower():
        return seed

    prefix_mods = {
        m.lower()
        for m in probe_cfg.get(
            "prefix_modifiers", ("link", "login", "daftar", "situs", "web")
        )
    }
    use_prefix = mod.lower() in prefix_mods and random.random() < 0.5
    return f"{mod} {seed}" if use_prefix else f"{seed} {mod}"


def clean_suggestion(
    s: str,
    min_len: int = 4,
    max_len: int = 72,
    reject_prefixes: tuple[str, ...] = ("http://", "https://"),
) -> str | None:
    clean = norm_space(s).lower()
    if not (min_len <= len(clean) <= max_len):
        return None
    if clean.startswith(reject_prefixes):
        return None
    return clean
