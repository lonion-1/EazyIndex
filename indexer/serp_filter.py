# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import difflib
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

RE_WA = re.compile(
    r"(?:wa\.me/|api\.whatsapp\.com/send\?[^\s>]*?phone=)\+?(\d{8,15})", re.IGNORECASE
)
RE_TG = re.compile(
    r"(?:t\.me|telegram\.me)/(?!share|joinchat|s/|\+)([A-Za-z0-9_]{4,32})",
    re.IGNORECASE,
)
RE_PAYMENT = re.compile(
    r"(?:transfer|rekening|no\.?\s*rek|norek|a\.?n\.?)[^.\n]{0,50}?\b\d{8,16}\b|"
    r"\b(?:dana|ovo|gopay|shopeepay|linkaja)\b[^.\n]{0,30}\b08\d{8,11}\b",
    re.IGNORECASE,
)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class SerpHit:
    url: str
    title: str
    snippet: str
    query: str
    category: str


def evaluate_serp_hit(
    hit: SerpHit, policy, cat_cfg: dict, brands: list[dict]
) -> dict | None:
    """Saring hasil SERP sebelum disentuh crawler: kembalikan dict kandidat atau None."""
    sp = urllib.parse.urlsplit(hit.url.strip())
    raw_host = (sp.hostname or "").lower()
    if not raw_host:
        return None

    # 1. Pisahkan shortener & link-in-bio (passthrough) untuk dilempar ke chainlink
    if policy.is_shortener(raw_host):
        return {"kind": "shortlink", "url": hit.url, "cat": hit.category, "s": 3}

    reg = policy.registrable(raw_host)
    if not reg or policy.is_protected(reg) or policy.is_ignored(reg):
        return None
    if policy.is_passthrough(reg):
        return {
            "kind": "passthrough",
            "url": hit.url,
            "d": reg,
            "cat": hit.category,
            "s": 4,
        }

    head_txt = hit.title.lower()
    body_txt = hit.snippet.lower()
    full_txt = f"{head_txt} {body_txt}"

    # 2. Penangkal konteks (veto berita/skripsi -> buang & laporkan ke telemetri query)
    veto_hits = [w for w in cat_cfg.get("context_veto", []) if w in full_txt]
    if veto_hits:
        return {"kind": "veto", "d": reg, "query": hit.query, "veto": veto_hits}

    # 3. Pemisahan lapisan NAME, HEAD, ACTION, SNIPPET + aturan function_requires_identity
    label = reg.split(".")[0]
    id_terms = cat_cfg.get("identity", [])
    fn_terms = cat_cfg.get("function", [])
    lure_terms = cat_cfg.get("lure", [])

    name_hits = [w for w in id_terms if w in label]
    head_id = [w for w in id_terms if w in head_txt]
    body_id = [w for w in id_terms if w in body_txt]
    has_identity = bool(name_hits or head_id or body_id)

    fn_hits = (
        [w for w in fn_terms if w in full_txt] if (has_identity or not id_terms) else []
    )
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

    # 4. Sinyal kontak WA/TG & nomor rekening/e-wallet langsung di cuplikan SERP
    if RE_WA.search(hit.snippet) or RE_TG.search(hit.snippet):
        score += 2
        reasons.append("serp_contact:wa/tg")
    if RE_PAYMENT.search(hit.snippet):
        score += 3
        reasons.append("serp_payment:rekening/ewallet")

    # 5. Peniruan merek (phishing) dengan pelindung kata ambigu (mis. 'dana')
    for b in brands:
        if any(reg == od or reg.endswith("." + od) for od in b.get("domains", [])):
            return None
        b_names = [n.lower() for n in b.get("names", [])]
        in_name = any(n.replace(" ", "") in label for n in b_names if len(n) >= 3)
        in_head = any(n in head_txt for n in b_names)
        if in_name or (in_head and not b.get("ambiguous", False)):
            score += 3
            reasons.append(f"brand:{b['key']}")
            break
        for od in b.get("domains", []):
            ol = od.split(".")[0]
            if len(ol) >= 5 and len(label) >= 5 and label != ol:
                if difflib.SequenceMatcher(None, label, ol).ratio() >= 0.84:
                    score += 3
                    reasons.append(f"typosquat:{od}")
                    break

    if score < int(cat_cfg.get("min_serp_score", 5)):
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
