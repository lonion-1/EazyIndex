# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Locale detection berbasis rule dari TOML (tanpa regex hardcode)."""
from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LocaleRule:
    id: str
    locale: tuple[str, str]
    ranges: tuple[tuple[int, int], ...] = ()
    chars: frozenset[str] = field(default_factory=frozenset)

    def matches(self, text: str) -> bool:
        if self.chars:
            low = text.lower()
            if any(c in low for c in self.chars):
                return True
        for start, end in self.ranges:
            for ch in text:
                if start <= ord(ch) <= end:
                    return True
        return False


@dataclass(frozen=True)
class LocaleDetector:
    rules: tuple[LocaleRule, ...]
    default_weighted: tuple[tuple[str, str], ...]

    @classmethod
    def from_config(cls, cfg: dict) -> "LocaleDetector":
        rules = tuple(
            LocaleRule(
                id=r["id"],
                locale=tuple(r["locale"]),
                ranges=tuple((int(a), int(b)) for a, b in r.get("ranges", [])),
                chars=frozenset(r.get("chars", "").lower()),
            )
            for r in cfg.get("Rules", [])
        )
        default = tuple(tuple(p) for p in cfg["default_weighted"])
        return cls(rules=rules, default_weighted=default)

    def detect(self, seed: str) -> tuple[str, str]:
        for rule in self.rules:
            if rule.matches(seed):
                return rule.locale
        return random.choice(self.default_weighted)
