# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: MIT
"""OSINT quality gate: buang noise, tegakkan portal/brand signal."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable


@dataclass(frozen=True)
class LocaleFilter:
    noise_words: frozenset[str] = frozenset()
    noise_regexes: tuple[re.Pattern, ...] = ()
    portal_keywords: frozenset[str] = frozenset()
    portal_tlds: frozenset[str] = frozenset()

    @classmethod
    def from_dict(cls, d: dict) -> "LocaleFilter":
        return cls(
            noise_words=frozenset(w.lower() for w in d.get("noise_words", [])),
            noise_regexes=tuple(
                re.compile(p, re.IGNORECASE) for p in d.get("noise_regexes", [])
            ),
            portal_keywords=frozenset(w.lower() for w in d.get("portal_keywords", [])),
            portal_tlds=frozenset(w.lower() for w in d.get("portal_tlds", [])),
        )

    def merge(self, other: "LocaleFilter") -> "LocaleFilter":
        return LocaleFilter(
            noise_words=self.noise_words | other.noise_words,
            noise_regexes=self.noise_regexes + other.noise_regexes,
            portal_keywords=self.portal_keywords | other.portal_keywords,
            portal_tlds=self.portal_tlds | other.portal_tlds,
        )


@dataclass(frozen=True)
class FilterEngine:
    base: LocaleFilter
    by_locale: dict[str, LocaleFilter] = field(default_factory=dict)
    strict_categories: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, cfg: dict) -> "FilterEngine":
        base = LocaleFilter.from_dict(cfg)
        by_locale = {
            code: LocaleFilter.from_dict(spec)
            for code, spec in cfg.get("by_locale", {}).items()
        }
        strict = frozenset(cfg.get("strict_categories", {}).get("categories", []))
        return cls(base=base, by_locale=by_locale, strict_categories=strict)

    def for_locale(self, hl: str) -> LocaleFilter:
        return self.base.merge(self.by_locale.get(hl, LocaleFilter()))

    # ------------------------------------------------------------
    # Pemeriksaan
    # ------------------------------------------------------------
    def has_noise(self, text: str, lf: LocaleFilter) -> bool:
        low = text.lower()
        # noise word: cocok sebagai kata utuh atau frasa substring
        for w in lf.noise_words:
            if w in low:
                return True
        for rx in lf.noise_regexes:
            if rx.search(text):
                return True
        return False

    def has_portal_signal(self, text: str, lf: LocaleFilter) -> bool:
        low = text.lower()
        tokens = re.split(
            r"[^0-9a-z\u00C0-\u024F\u0E00-\u0E7F\u3040-\u30FF\u4E00-\u9FAF\uAC00-\uD7AF]+",
            low,
        )
        token_set = set(t for t in tokens if t)
        # 1) keyword portal utuh
        for kw in lf.portal_keywords:
            if " " in kw:
                if kw in low:
                    return True
            elif kw in token_set:
                return True
        # 2) TLD sebagai token
        for tld in lf.portal_tlds:
            if "." in tld:  # co.jp dsb — cek substring
                if tld in low:
                    return True
            elif tld in token_set:
                return True
        return False

    def accept(
        self,
        text: str,
        *,
        cat: str,
        hl: str,
        brand_tokens: Iterable[str] = (),
        require_portal_or_brand: bool = True,
        min_words: int = 1,
        max_words: int = 5,
        min_len: int = 4,
        max_len: int = 48,
        reject_prefixes: Iterable[str] = ("http://", "https://"),
    ) -> bool:
        low = text.lower().strip()

        # 1) panjang & format
        if not (min_len <= len(low) <= max_len):
            return False
        if low.startswith(tuple(reject_prefixes)):
            return False
        word_count = len(low.split())
        if not (min_words <= word_count <= max_words):
            return False

        lf = self.for_locale(hl)

        # 2) noise
        if self.has_noise(low, lf):
            return False

        # 3) portal / brand requirement
        strict = cat in self.strict_categories
        has_portal = self.has_portal_signal(low, lf)
        has_brand = any(b.lower() in low for b in brand_tokens if b)

        if strict:
            # kategori strict WAJIB ada sinyal portal (brand saja tidak cukup)
            return has_portal
        if require_portal_or_brand:
            return has_portal or has_brand
        return True


@lru_cache(maxsize=1)
def _noop() -> None:  # pragma: no cover - placeholder
    return None
