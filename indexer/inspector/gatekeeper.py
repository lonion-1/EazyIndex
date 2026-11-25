# Copyright (c) 2026 EazyWhale
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contrastive 4-Pillar Gatekeeper (Legal vs. Illegal):
- Menimbang Bukti Legal (RS, toko, berita, kampus, platform resmi) vs Bukti Ilegal across 4 pilar.
- Mendukung Adaptive +1 Sample Page Probe saat halaman depan disamarkan atau meragukan.
"""

from __future__ import annotations

import re
import urllib.parse

from ..config_loader import CategoryRule, IndexerConfig
from ..models import CategoryScore, GateVerdict, LegalAssessment, PageZones
from ..text_utils import (
    deobfuscate,
    extract_safe_domain,
    is_subdomain_of,
    norm_space,
    registrable_domain,
)

RE_CORP_ENTITY = re.compile(
    r"©\s*(?:19|20)\d\d\s+[A-Z][\w&.,\- ]{2,45}|"
    r"\b(?:PT|CV|Inc\.?|Ltd\.?|LLC|GmbH|Corp\.?|Sdn\.?\s*Bhd\.?|Pte\.?\s*Ltd\.?|RSUD|RSU)\b"
)
RE_PHONE_OR_ADDR = re.compile(
    r"(?:"
    r"(?:tel|telp|phone|fax|hotline|call\s*us)\s*[:.]?\s*\+?\d[\d\s\-().]{6,18}|"
    r"\b(?:1-800|0800|021|031|022|024|061|0274|0361)[-\s]?\d{3,4}[-\s]?\d{3,4}\b|"
    r"\b(?:jl\.|jalan)\s+[a-z0-9.\s]{4,35}(?:no\.|nomor|rt|rw|kota|kabupaten|jakarta|bandung|surabaya)"
    r")",
    re.IGNORECASE,
)


def _merge_zones(primary: PageZones, secondary: PageZones) -> PageZones:
    """Gabungkan temuan halaman utama dengan +1 halaman sampel tanpa menghilangkan konteks."""
    return PageZones(
        final_url=primary.final_url,
        title=primary.title or secondary.title,
        meta=norm_space(f"{primary.meta} {secondary.meta}")[:800],
        head_text=norm_space(f"{primary.head_text} {secondary.head_text}")[:1200],
        nav_text=norm_space(f"{primary.nav_text} {secondary.nav_text}")[:3500],
        main_text=norm_space(f"{primary.main_text} {secondary.main_text}")[:24000],
        footer_text=norm_space(f"{primary.footer_text} {secondary.footer_text}")[:4000],
        full_text=norm_space(f"{primary.full_text} {secondary.full_text}")[:30000],
        raw_html=primary.raw_html,
        jsonld_types=primary.jsonld_types | secondary.jsonld_types,
        nav_links=list(dict.fromkeys(primary.nav_links + secondary.nav_links)),
        internal_links=list(
            dict.fromkeys(primary.internal_links + secondary.internal_links)
        ),
        outbound_links=primary.outbound_links + secondary.outbound_links,
        iframes=list(dict.fromkeys(primary.iframes + secondary.iframes)),
        has_password_input=primary.has_password_input or secondary.has_password_input,
        form_text=norm_space(f"{primary.form_text} {secondary.form_text}")[:4000],
        script_markers=norm_space(
            f"{primary.script_markers} {secondary.script_markers}"
        )[:8000],
    )


def evaluate_legal(
    cfg: IndexerConfig, domain: str, zones: PageZones
) -> LegalAssessment:
    """Hitung bukti bahwa situs ini adalah entitas dunia nyata yang sah (Legal)."""
    lcfg = cfg.legal
    res = LegalAssessment()

    if any(domain.endswith(tld) for tld in lcfg.institutional_tlds):
        res.score += 8
        res.is_institutional_tld = True
        res.reasons.append("legal:institutional_tld")

    matched_ld = sorted(zones.jsonld_types & lcfg.jsonld_benign_types)
    if matched_ld:
        res.score += 4
        res.reasons.append(f"legal:jsonld({','.join(matched_ld[:3])})")

    full_low = zones.full_text.lower()
    if lcfg.re_news_reporting:
        news_hits = {
            m.group(0).lower() for m in lcfg.re_news_reporting.finditer(full_low)
        }
        if news_hits:
            res.has_passive_news_context = True
            res.score += min(4, len(news_hits) * 2)
            res.reasons.append(f"legal:news_context({len(news_hits)})")

    footer_and_tail = f"{zones.footer_text} {zones.main_text[-3000:]}"
    if RE_CORP_ENTITY.search(footer_and_tail):
        res.score += 2
        res.reasons.append("legal:corp_entity")
    if RE_PHONE_OR_ADDR.search(footer_and_tail):
        res.score += 2
        res.reasons.append("legal:physical_contact")

    nav_set = " ".join(zones.nav_links)
    for pname, prof in lcfg.profiles.items():
        if prof.official_domains and is_subdomain_of(domain, prof.official_domains):
            res.score += 8
            res.matched_profiles.append(pname)
            res.reasons.append(f"legal:official_domain({pname})")
            continue

        nav_matches = sum(1 for np in prof.nav_paths if np in nav_set)
        body_matches = 0
        if prof.re_body_markers:
            body_matches = len(
                {m.group(0).lower() for m in prof.re_body_markers.finditer(full_low)}
            )

        if (
            nav_matches >= 2
            or body_matches >= 2
            or (nav_matches >= 1 and body_matches >= 1)
        ):
            res.score += prof.weight
            res.matched_profiles.append(pname)
            res.reasons.append(
                f"legal:profile_{pname}(nav={nav_matches},body={body_matches})"
            )

    return res


def evaluate_category_pillars(
    cfg: IndexerConfig,
    domain: str,
    zones: PageZones,
    legal: LegalAssessment,
    rule: CategoryRule,
    hint_cat: str = "",
) -> CategoryScore:
    """Evaluasi 4 Pilar Ilegal untuk satu kategori tertentu."""
    cs = CategoryScore(category=rule.name)
    dom_variants = [domain] + deobfuscate(domain, 200)

    # ── PILAR 1: Domain & Jejak Asal (P1_DOMAIN) ──
    p1_pts = 0
    if rule.re_domain_hints and any(
        rule.re_domain_hints.search(v) for v in dom_variants
    ):
        p1_pts += 3
        cs.reasons.append("P1:domain_hint")
    if rule.re_mirror_patterns and any(
        rule.re_mirror_patterns.search(v) for v in dom_variants
    ):
        p1_pts += 2
        cs.reasons.append("P1:mirror_pattern")
    if any(domain.endswith(tld) for tld in rule.high_risk_tlds):
        p1_pts += 1
        cs.reasons.append("P1:high_risk_tld")
    if hint_cat == rule.name and p1_pts > 0:
        p1_pts += 1
    if p1_pts >= 2:
        cs.pillars_hit.append("P1_DOMAIN")
        cs.illegal_score += min(5, p1_pts)

    # ── PILAR 2: Niat Semantik Utuh di Seluruh Halaman (P2_SEMANTIC) ──
    full_low = zones.full_text.lower()
    cat_passive = bool(
        rule.re_passive_context and rule.re_passive_context.search(full_low)
    )

    if not legal.has_passive_news_context and not cat_passive and rule.re_active_intent:
        head_nav_variants = deobfuscate(f"{zones.head_text} {zones.nav_text}", 3500)
        main_variants = deobfuscate(zones.main_text, 12000)

        hn_hits: set[str] = set()
        for v in head_nav_variants:
            hn_hits.update(
                m.group(0).lower() for m in rule.re_active_intent.finditer(v)
            )

        main_hits: set[str] = set()
        for v in main_variants:
            main_hits.update(
                m.group(0).lower() for m in rule.re_active_intent.finditer(v)
            )

        total_sem = len(hn_hits | main_hits)
        if total_sem >= 2 or (len(hn_hits) >= 1 and len(main_hits) >= 1):
            sem_pts = min(6, len(hn_hits) * 2 + len(main_hits))
            cs.pillars_hit.append("P2_SEMANTIC")
            cs.illegal_score += sem_pts
            cs.reasons.append(f"P2:intent(hn={len(hn_hits)},main={len(main_hits)})")

    # ── PILAR 3: Anatomi & Struktur Fungsional DOM (P3_ANATOMY) ──
    p3_pts = 0
    if rule.re_url_paths and zones.internal_links:
        matching_paths = sum(
            1
            for u in zones.internal_links[:200]
            if rule.re_url_paths.search(urllib.parse.urlparse(u).path)
        )
        if matching_paths >= 2:
            p3_pts += 3
            cs.reasons.append(f"P3:url_paths({matching_paths})")
        elif matching_paths == 1:
            p3_pts += 1

    dom_blob = f"{zones.nav_text} {zones.main_text} {zones.form_text}".lower()
    if rule.re_dom_signatures:
        sig_hits = {
            m.group(0).lower() for m in rule.re_dom_signatures.finditer(dom_blob)
        }
        if len(sig_hits) >= 2:
            p3_pts += min(4, len(sig_hits))
            cs.reasons.append(f"P3:dom_sigs({len(sig_hits)})")

    ext_iframes = [
        src
        for src in zones.iframes
        if registrable_domain(urllib.parse.urlparse(src).hostname or "", cfg.multi_tlds)
        not in ("", domain)
    ]
    if rule.name in ("film", "porn") and ext_iframes:
        p3_pts += 2
        cs.reasons.append(f"P3:ext_iframe({len(ext_iframes)})")

    if rule.name == "phishing" and zones.has_password_input:
        p3_pts += 3
        cs.reasons.append("P3:password_form")

    if p3_pts >= 3:
        cs.pillars_hit.append("P3_ANATOMY")
        cs.illegal_score += min(6, p3_pts)

    # ── PILAR 4: Ekosistem Tautan Keluar & Jaringan Iklan (P4_ECOSYSTEM) ──
    p4_pts = 0
    total_links = len(zones.internal_links) + len(zones.outbound_links)
    if total_links >= 5:
        out_ratio = len(zones.outbound_links) / total_links
        if out_ratio >= rule.outbound_ratio_threshold:
            p4_pts += 2
            cs.reasons.append(f"P4:out_ratio({out_ratio:.2f})")

    banner_ads = 0
    suspect_out = 0
    for out_url, anchor_ctx, has_img in zones.outbound_links[:250]:
        out_dom = registrable_domain(
            urllib.parse.urlparse(out_url).hostname or "", cfg.multi_tlds
        )
        if not out_dom or is_subdomain_of(out_dom, cfg.allowlist):
            continue
        blob_out = f"{out_dom} {anchor_ctx}".lower()
        if (
            rule.re_suspect_outbound
            and rule.re_suspect_outbound.search(blob_out)
            or cfg.re_any_illegal_domain
            and cfg.re_any_illegal_domain.search(out_dom)
        ):
            suspect_out += 1
        if has_img and rule.re_ad_markers and rule.re_ad_markers.search(blob_out):
            banner_ads += 1

    if suspect_out >= 1:
        p4_pts += min(3, suspect_out + 1)
        cs.reasons.append(f"P4:suspect_out({suspect_out})")
    if banner_ads >= 1:
        p4_pts += 2
        cs.reasons.append(f"P4:banner_ads({banner_ads})")

    ad_blob = f"{zones.script_markers} {zones.full_text[-4000:]}".lower()
    if rule.re_ad_markers:
        ad_hits = {m.group(0).lower() for m in rule.re_ad_markers.finditer(ad_blob)}
        if ad_hits:
            p4_pts += min(2, len(ad_hits))
            cs.reasons.append(f"P4:ad_markers({len(ad_hits)})")

    if p4_pts >= 2:
        cs.pillars_hit.append("P4_ECOSYSTEM")
        cs.illegal_score += min(5, p4_pts)

    return cs


def pick_adaptive_probe_url(cfg: IndexerConfig, domain: str, zones: PageZones) -> str:
    """Pilih maksimal 1 tautan internal terbaik untuk uji lanjut bila halaman utama ambigu."""
    if not zones.internal_links:
        return ""

    if cfg.legal.re_action_path:
        for u in zones.internal_links:
            path = urllib.parse.urlparse(u).path
            if path and path != "/" and cfg.legal.re_action_path.search(path):
                return u

    for u in zones.internal_links:
        path = urllib.parse.urlparse(u).path.lower().rstrip("/")
        if any(path == ip or path.endswith(ip) for ip in cfg.legal.identity_paths):
            return u

    return ""


def _collect_outbound_domains(
    cfg: IndexerConfig, domain: str, zones: PageZones
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for out_url, _, _ in zones.outbound_links:
        d = extract_safe_domain(
            out_url, cfg.multi_tlds, cfg.allowlist, cfg.legal.institutional_tlds
        )
        if d and d != domain and d not in seen:
            seen.add(d)
            out.append(d)
            if len(out) >= 25:
                break
    return out


def decide_gate_verdict(
    cfg: IndexerConfig,
    domain: str,
    landing_zones: PageZones,
    hint_cat: str = "",
    secondary_zones: PageZones | None = None,
    probed_url: str = "",
) -> GateVerdict:
    """Jalankan keputusan akhir Contrastive 4-Pillar Gatekeeper."""
    zones = (
        _merge_zones(landing_zones, secondary_zones)
        if secondary_zones is not None
        else landing_zones
    )
    legal = evaluate_legal(cfg, domain, zones)

    best_cs: CategoryScore | None = None
    best_rule: CategoryRule | None = None

    for cname, rule in cfg.categories.items():
        cs = evaluate_category_pillars(
            cfg, domain, zones, legal, rule, hint_cat=hint_cat
        )
        if best_cs is None or (len(cs.pillars_hit), cs.illegal_score) > (
            len(best_cs.pillars_hit),
            best_cs.illegal_score,
        ):
            best_cs = cs
            best_rule = rule

    assert best_cs is not None and best_rule is not None
    out_doms = _collect_outbound_domains(cfg, domain, zones)

    has_strong_anatomy = (
        "P3_ANATOMY" in best_cs.pillars_hit and best_cs.illegal_score >= 9
    )

    if legal.is_institutional_tld or (
        legal.score >= best_rule.veto_legal_threshold and not has_strong_anatomy
    ):
        return GateVerdict(
            decision="VETO_LEGAL",
            domain=domain,
            landing_url=zones.final_url,
            category=best_cs.category,
            illegal_score=best_cs.illegal_score,
            legal_score=legal.score,
            pillars=best_cs.pillars_hit,
            reasons=best_cs.reasons,
            legal_reasons=legal.reasons,
            probed_subpage=probed_url,
            outbound_domains=out_doms,
            title=zones.title,
        )

    if (
        len(best_cs.pillars_hit) >= best_rule.min_pillars
        and best_cs.illegal_score >= best_rule.min_illegal_score
        and (best_cs.illegal_score - legal.score) >= 3
    ):
        return GateVerdict(
            decision="ACCEPT",
            domain=domain,
            landing_url=zones.final_url,
            category=best_cs.category,
            illegal_score=best_cs.illegal_score,
            legal_score=legal.score,
            pillars=best_cs.pillars_hit,
            reasons=best_cs.reasons,
            legal_reasons=legal.reasons,
            probed_subpage=probed_url,
            outbound_domains=out_doms,
            title=zones.title,
        )

    if (
        len(best_cs.pillars_hit) >= 2
        and best_cs.illegal_score >= 5
        and legal.score < best_rule.veto_legal_threshold
    ):
        return GateVerdict(
            decision="REVIEW",
            domain=domain,
            landing_url=zones.final_url,
            category=best_cs.category,
            illegal_score=best_cs.illegal_score,
            legal_score=legal.score,
            pillars=best_cs.pillars_hit,
            reasons=best_cs.reasons,
            legal_reasons=legal.reasons,
            probed_subpage=probed_url,
            outbound_domains=out_doms,
            title=zones.title,
        )

    return GateVerdict(
        decision="REJECT",
        domain=domain,
        landing_url=zones.final_url,
        category=best_cs.category,
        illegal_score=best_cs.illegal_score,
        legal_score=legal.score,
        pillars=best_cs.pillars_hit,
        reasons=best_cs.reasons,
        legal_reasons=legal.reasons,
        probed_subpage=probed_url,
        outbound_domains=out_doms,
        title=zones.title,
    )


def needs_adaptive_probe(
    cfg: IndexerConfig, domain: str, landing_zones: PageZones, hint_cat: str = ""
) -> str:
    """Cek apakah halaman utama berada di zona abu-abu sehingga perlu mengambil +1 halaman sampel."""
    if not bool(cfg.run.get("adaptive_probe", True)):
        return ""
    initial = decide_gate_verdict(cfg, domain, landing_zones, hint_cat=hint_cat)
    if initial.decision in ("ACCEPT", "VETO_LEGAL"):
        return ""
    if len(initial.pillars) >= 1 or initial.illegal_score >= 3:
        return pick_adaptive_probe_url(cfg, domain, landing_zones)
    return ""
