# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: MIT
"""Tipe data dasar untuk filter SERP."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SerpHit:
    """Representasi satu hasil SERP sebelum disaring."""

    url: str
    title: str
    snippet: str
    query: str
    category: str


@dataclass(slots=True)
class SerpCandidate:
    """Hasil akhir filter yang siap dilempar ke chainlink/crawler."""

    kind: str  # "target", "shortlink", "passthrough"
    url: str
    domain: str
    category: str
    score: int
    trail: list[dict]
    bounces: int = 0
