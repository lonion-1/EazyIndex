# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Interface & implementasi default untuk domain policy."""

from __future__ import annotations

from abc import ABC, abstractmethod


class SerpPolicy(ABC):
    """Kontrak untuk manajemen domain (shortener, protected, dll)."""

    @abstractmethod
    def is_shortener(self, host: str) -> bool:
        ...

    @abstractmethod
    def registrable(self, host: str) -> str | None:
        ...

    @abstractmethod
    def is_protected(self, domain: str) -> bool:
        ...

    @abstractmethod
    def is_ignored(self, domain: str) -> bool:
        ...

    @abstractmethod
    def is_passthrough(self, domain: str) -> bool:
        ...


class DefaultPolicy(SerpPolicy):
    """Fallback sementara jika policy asli belum tersedia."""

    def __init__(self) -> None:
        self._shorteners = frozenset({
            "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "s.id",
            "linktr.ee", "carrd.co", "bio.link", "beacons.ai",
        })
        self._protected = frozenset({
            "google.com", "youtube.com", "wikipedia.org", "github.com",
            "facebook.com", "instagram.com", "twitter.com", "tiktok.com",
        })
        self._ignored = frozenset({"localhost"})
        self._passthrough = frozenset()

    def is_shortener(self, host: str) -> bool:
        return any(host == s or host.endswith("." + s) for s in self._shorteners)

    def registrable(self, host: str) -> str | None:
        # Sangat sederhana: ambil 2 label terakhir (tidak akurat untuk ccTLD ganda, tapi cukup untuk fallback)
        parts = host.lower().split(".")
        if len(parts) < 2:
            return None
        return ".".join(parts[-2:])

    def is_protected(self, domain: str) -> bool:
        return any(domain == p or domain.endswith("." + p) for p in self._protected)

    def is_ignored(self, domain: str) -> bool:
        return domain in self._ignored

    def is_passthrough(self, domain: str) -> bool:
        return domain in self._passthrough
