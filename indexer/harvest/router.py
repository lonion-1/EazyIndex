# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: MIT
"""Weighted provider routing berdasarkan locale (gl)."""

from __future__ import annotations

import random


def choose_source(
    gl: str,
    routing_default: dict[str, float],
    routing_override: dict[str, dict[str, float]],
    available: set[str],
    disabled: set[str],
) -> str | None:
    table = routing_override.get(gl, routing_default)
    active = [
        (src, w)
        for src, w in table.items()
        if src in available and src not in disabled and w > 0
    ]
    if not active:
        return None
    names, weights = zip(*active)
    return random.choices(names, weights=weights, k=1)[0]
