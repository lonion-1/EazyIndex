# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build probe query & clean suggestion."""

from __future__ import annotations

import random
from typing import Any

from ..text_utils import norm_space


def build_probe_query(
    seed: str,
    cat: str,
    year: str,
    probe_cfg: dict[str, Any],
    modifiers: dict[str, dict[str, list[str]]],
) -> str:
    r = random.random()
    bare_p = probe_cfg.get("bare_prob", 0.55)
    year_p = probe_cfg.get("year_prob", 0.80)

    if r < bare_p:
        return seed
    if r < year_p:
        return f"{seed} {year}"

    mod_cfg = modifiers.get(cat)
    if not mod_cfg:
        mod = probe_cfg.get("fallback_modifier", "terbaru")
        return f"{seed} {mod}".strip()

    use_prefix = random.random() < 0.5
    pool = mod_cfg["prefix"] if use_prefix else mod_cfg["suffix"]
    mod = random.choice(pool).replace("{year}", year)
    if not mod or mod in seed.lower():
        return seed
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
