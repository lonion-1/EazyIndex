# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine utama untuk menyaring hasil SERP berdasarkan konfigurasi TOML."""

from __future__ import annotations

import difflib
import urllib.parse
from functools import lru_cache

from .patterns import RE_PAYMENT, RE_TG, RE_WA
from .policy import DefaultPolicy, SerpPolicy
from .types import SerpHit


@lru_cache(maxsize=1024)
def _get_domain_labels(host: str) -> str:
    return host.split(".")[0]


def evaluate_serp_hit(
    hit: SerpHit,
    cfg_cat: dict,
    brands: list[dict],
    policy: SerpPolicy | None = None,
) -> dict | None:
    """Saring hasil SERP. Kembalikan dict kandidat atau None jika dibuang."""
    pol = policy or DefaultPolicy()

    sp = urllib.parse.urlsplit(hit.url.strip())
    raw_host = (sp.hostname or "").lower()
    if not raw_host:
        return None

    # 1. Shortener / Passthrough
    if pol.is_shortener(raw_host):
        return {"kind": "shortlink", "url": hit.url, "cat": hit.category, "s": 3}

    reg = pol.registrable(raw_host)
    if not reg or pol.is_protected(reg) or pol.is_ignored(reg):
        return None
    if pol.is_passthrough(reg):
        return {"kind": "passthrough", "url": hit.url, "d": reg, "cat": hit.category, "s": 4}

    # Ambil data dari [serp_filter] (jika ada)
    sf = cfg_cat.get("serp_filter", {})
    min_score = int(sf.get("min_serp_score", cfg_cat.get("min_serp_score", 5)))
    id_terms = sf.get("identity", cfg_cat.get("identity", []))
    fn_terms = sf.get("function", cfg_cat.get("function", []))
    lure_terms = sf.get("lure", cfg_cat.get("lure", []))
    veto_terms = sf.get("context_veto", cfg_cat.get("context_veto", []))

    head_txt = hit.title.lower()
    body_txt = hit.snippet.lower()
    full_txt = f"{head_txt} {body_txt}"

    # 2. Veto Context (Langsung buang jika ada kata pengaman)
    if any(w in full_txt for w in veto_terms):
        return None

    # 3. Scoring
    label = _get_domain_labels(reg)
    name_hits = [w for w in id_terms if w in label]
    head_id = [w for w in id_terms if w in head_txt]
    body_id = [w for w in id_terms if w in body_txt]
    has_identity = bool(name_hits or head_id or body_id)

    fn_hits = [w for w in fn_terms if w in full_txt] if (has_identity or not id_terms) else []
    lure_hits = [w for w in lure_terms if w in full_txt]

    score = 0
    reasons: list[str] = []
    if name_hits:
        score += 2
        reasons.append(f"name:{','.join(name_hits[:3])}")
    if head_id:
        score += 3
        reasons.append(f"head:{','.join(head_id[:3])}")
    if body_id or lure_hits:
        score += 2
        reasons.append(f"snippet:{','.join((body_id + lure_hits)[:3])}")
    if fn_hits:
        score += 2
        reasons.append(f"action:{','.join(fn_hits[:3])}")

    # 4. Sinyal Kontak / Payment
    if RE_WA.search(hit.snippet) or RE_TG.search(hit.snippet):
        score += 2
        reasons.append("serp_contact:wa/tg")
    if RE_PAYMENT.search(hit.snippet):
        score += 3
        reasons.append("serp_payment:rekening/ewallet")

    # 5. Brand / Typosquatting
    for b in brands:
        if any(reg == od or reg.endswith("." + od) for od in b.get("domains", [])):
            return None
        b_names = [n.lower() for n in b.get("names", [])]
        in_name = any(n.replace(" ", "") in label for n in b_names if len(n) >= 3)
        in_head = any(n in head_txt for n in b_names)
        if in_name or (in_head and not b.get("ambiguous", False)):
            score += 3
            reasons.append(f"brand:{b.get('key', 'unknown')}")
            break
        for od in b.get("domains", []):
            ol = od.split(".")[0]
            if len(ol) >= 5 and len(label) >= 5 and label != ol:
                if difflib.SequenceMatcher(None, label, ol).ratio() >= 0.84:
                    score += 3
                    reasons.append(f"typosquat:{od}")
                    break

    if score < min_score:
        return None

    return {
        "kind": "target",
        "d": reg,
        "url": hit.url,
        "cat": hit.category,
        "s": score,
        "bounces": 0,
        "trail": [{"bot": "web-index", "act": "serp_pass", "note": "; ".join(reasons)}],
      }
