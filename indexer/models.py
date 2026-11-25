# Copyright (c) 2026 EazyWhale
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model data terpadu untuk seluruh alur Front-Gate Indexer."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Lead:
    """Satu entri antrean URL/domain yang akan diperiksa di Front Gate."""
    url: str
    domain: str
    priority: float
    via: str
    hint_cat: str = ""
    snippet: str = ""


@dataclass
class PageZones:
    """Hasil pembedahan 5 zona halaman HTML utuh."""
    final_url: str = ""
    title: str = ""
    meta: str = ""
    head_text: str = ""
    nav_text: str = ""
    main_text: str = ""
    footer_text: str = ""
    full_text: str = ""
    raw_html: str = ""
    jsonld_types: set[str] = field(default_factory=set)
    nav_links: list[str] = field(default_factory=list)
    internal_links: list[str] = field(default_factory=list)
    outbound_links: list[tuple[str, str, bool]] = field(default_factory=list)  # (url, anchor_ctx, has_img)
    iframes: list[str] = field(default_factory=list)
    has_password_input: bool = False
    form_text: str = ""
    script_markers: str = ""


@dataclass
class LegalAssessment:
    """Hasil evaluasi kontra-bukti entitas sah (Legal Profiles)."""
    score: int = 0
    matched_profiles: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    is_institutional_tld: bool = False
    has_passive_news_context: bool = False


@dataclass
class CategoryScore:
    """Evaluasi 4 pilar untuk satu kategori target tertentu."""
    category: str
    pillars_hit: list[str] = field(default_factory=list)
    illegal_score: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class GateVerdict:
    """Keputusan akhir Front-Gate Indexer untuk satu domain."""
    decision: str  # "ACCEPT" | "REVIEW" | "VETO_LEGAL" | "REJECT"
    domain: str
    landing_url: str
    category: str = ""
    illegal_score: int = 0
    legal_score: int = 0
    pillars: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    legal_reasons: list[str] = field(default_factory=list)
    probed_subpage: str = ""
    outbound_domains: list[str] = field(default_factory=list)
    title: str = ""
