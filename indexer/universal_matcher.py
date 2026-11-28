# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: MIT
"""Universal Multi-Category Matcher:
Menggabungkan aturan dari IndexerConfig (categories/*.toml) + config/domain_rules.toml
agar semua kategori aktif terdeteksi sejak gerbang masuk.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

try:
    from indexer.text_utils import deobfuscate
except Exception:

    def deobfuscate(text: str, max_len: int = 4000) -> list[str]:
        s = " ".join((text or "")[:max_len].lower().split())
        return [s] if s else []


_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent if _HERE.name == "scripts" else _HERE
_EXT_CACHE: dict[tuple[str, float], dict] = {}
_RULES_CACHE: dict[tuple[int, float], dict[str, dict]] = {}

_LEET_MAP = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "8": "b",
        "@": "a",
        "$": "s",
    }
)


def leet_variants(domain: str) -> list[str]:
    d = (domain or "").strip().lower()
    if not d:
        return []
    out = [d]
    tr = d.translate(_LEET_MAP)
    if tr != d:
        out.append(tr)
    collapsed = re.sub(r"(?<=[a-z0-9])[._-]+(?=[a-z0-9])", "", tr)
    if collapsed != tr and len(collapsed) >= 4:
        out.append(collapsed)
    return list(dict.fromkeys(out))


def ctx_judol_count(cfg, clean_ctx: str) -> int:
    if not clean_ctx:
        return 0
    re_strong = getattr(cfg, "re_strong", None)
    re_moderate = getattr(cfg, "re_moderate", None)
    if re_strong is not None or re_moderate is not None:
        hits = len(re_strong.findall(clean_ctx)) * 2 if re_strong else 0
        hits += len(re_moderate.findall(clean_ctx)) if re_moderate else 0
        return hits

    cats = getattr(cfg, "categories", None) or {}
    judol_rule = cats.get("judol")
    if judol_rule is None:
        return 0
    hits = 0
    if judol_rule.re_active_intent is not None:
        hits += len(judol_rule.re_active_intent.findall(clean_ctx)) * 2
    if judol_rule.re_dom_signatures is not None:
        hits += len(judol_rule.re_dom_signatures.findall(clean_ctx))
    return hits


def _find_domain_rules_path() -> Path | None:
    for p in (
        ROOT / "config" / "domain_rules.toml",
        ROOT / "config" / "Domain_rules.toml",
        ROOT / "domain_rules.toml",
        ROOT / "Domain_rules.toml",
    ):
        if p.exists():
            return p
    return None


def _compile_word_list(words: list[str]) -> re.Pattern | None:
    parts: list[str] = []
    for w in words or []:
        s = str(w).strip().lower()
        if not s:
            continue
        esc = re.escape(s)
        if len(s) <= 4 and s.isascii():
            parts.append(rf"(?:^|[.-]){esc}(?:[.-]|$)")
        else:
            parts.append(esc)
    if not parts:
        return None
    try:
        return re.compile("|".join(parts), re.IGNORECASE)
    except re.error:
        return None


def _compile_raw_regex(patterns: list[str]) -> re.Pattern | None:
    valid: list[str] = []
    for p in patterns or []:
        s = str(p).strip()
        if not s:
            continue
        try:
            re.compile(s, re.IGNORECASE)
            valid.append(f"(?:{s})")
        except re.error:
            continue
    if not valid:
        return None
    try:
        return re.compile("|".join(valid), re.IGNORECASE)
    except re.error:
        return None


def _load_external_domain_rules(path: Path | None, mtime: float) -> dict:
    if path is None:
        return {}
    key = (str(path), mtime)
    if key in _EXT_CACHE:
        return _EXT_CACHE[key]
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except Exception:
        data = {}
    _EXT_CACHE[key] = data
    return data


def get_universal_rules(cfg) -> dict[str, dict]:
    rules_path = _find_domain_rules_path()
    mtime = rules_path.stat().st_mtime if rules_path else 0.0
    cache_key = (id(cfg), mtime)
    if cache_key in _RULES_CACHE:
        return _RULES_CACHE[cache_key]

    ext = _load_external_domain_rules(rules_path, mtime)
    cfg_cats = getattr(cfg, "categories", None) or {}
    idx = getattr(cfg, "index", None) or {}
    idx_cats = (
        idx.get("categories", {}) if isinstance(idx.get("categories"), dict) else {}
    )

    all_cats = sorted(set(ext.keys()) | set(cfg_cats.keys()) | set(idx_cats.keys()))
    merged: dict[str, dict] = {}

    for cat in all_cats:
        if cat == "judol" and getattr(cfg, "re_strong", None) is not None:
            continue
        d_sec = ext.get(cat) if isinstance(ext.get(cat), dict) else {}
        i_sec = idx_cats.get(cat) if isinstance(idx_cats.get(cat), dict) else {}
        c_rule = cfg_cats.get(cat)

        dom_strong = list(d_sec.get("strong", []) or []) + list(
            i_sec.get("domain_hint", []) or []
        )
        dom_rx = list(d_sec.get("strong_regex", []) or [])
        weak_words = list(d_sec.get("weak", []) or []) + list(
            i_sec.get("weak", []) or []
        )
        ctx_rx = list(d_sec.get("context_regex", []) or []) + list(
            i_sec.get("strong", []) or []
        )
        neg_words = list(d_sec.get("neg", []) or []) + list(
            i_sec.get("negative", []) or []
        )

        compiled_dom_strong = _compile_word_list(dom_strong) or (
            getattr(c_rule, "re_domain_hints", None) if c_rule else None
        )
        compiled_dom_rx = _compile_raw_regex(dom_rx) or (
            getattr(c_rule, "re_mirror_patterns", None) if c_rule else None
        )
        compiled_weak = _compile_word_list(weak_words) or (
            getattr(c_rule, "re_active_intent", None) if c_rule else None
        )
        compiled_ctx_rx = _compile_raw_regex(ctx_rx) or (
            getattr(c_rule, "re_dom_signatures", None) if c_rule else None
        )
        compiled_neg = _compile_raw_regex(neg_words) or (
            getattr(c_rule, "re_passive_context", None) if c_rule else None
        )

        default_min = getattr(c_rule, "min_illegal_score", 4) if c_rule else 4
        merged[cat] = {
            "min_score": int(
                d_sec.get("min_score", i_sec.get("min_score", default_min))
            ),
            "re_strong": compiled_dom_strong,
            "re_strong_rx": compiled_dom_rx,
            "re_dom_strong": compiled_dom_strong,
            "re_dom_rx": compiled_dom_rx,
            "re_weak": compiled_weak,
            "re_ctx_rx": compiled_ctx_rx,
            "re_neg": compiled_neg,
        }

    _RULES_CACHE[cache_key] = merged
    return merged


def match_universal(cfg, dom: str, context: str = "") -> tuple[str, int, list[str]]:
    """Evaluasi domain + teks konteks terhadap SEMUA kategori.
    Mengembalikan: (kategori_terbaik, skor_tertinggi, daftar_alasan).
    """
    d = (dom or "").strip().lower()
    variants = list(dict.fromkeys([d] + list(leet_variants(d))))
    clean_ctx = " ".join(deobfuscate(context[:12_000])) if context else ""

    best_cat = "judol"
    best_score = 0
    best_reasons: list[str] = []

    # 1. Evaluasi [judol] legacy jika cfg punya re_strong / re_moderate
    re_strong = getattr(cfg, "re_strong", None)
    re_moderate = getattr(cfg, "re_moderate", None)
    if re_strong is not None or re_moderate is not None:
        j_pts = 0
        j_reasons: list[str] = []
        if re_strong and any(re_strong.search(v) for v in variants):
            j_pts += 6
            j_reasons.append("judol:domain_strong")
        elif re_moderate and any(re_moderate.search(v) for v in variants):
            j_pts += 2
            j_reasons.append("judol:domain_mod")

        if clean_ctx:
            jh = ctx_judol_count(cfg, clean_ctx)
            if jh > 0:
                j_pts += min(6, jh * 2)
                j_reasons.append(f"judol:ctx({jh})")

        if j_pts > best_score:
            best_cat, best_score, best_reasons = "judol", j_pts, j_reasons

    # 2. Evaluasi semua kategori gabungan
    for cat_name, r in get_universal_rules(cfg).items():
        if r["re_neg"] and (
            r["re_neg"].search(d) or (clean_ctx and r["re_neg"].search(clean_ctx))
        ):
            continue

        pts = 0
        reasons: list[str] = []

        if r["re_dom_strong"] and any(r["re_dom_strong"].search(v) for v in variants):
            pts += 6
            reasons.append(f"{cat_name}:domain_strong")
        elif r["re_dom_rx"] and any(r["re_dom_rx"].search(v) for v in variants):
            pts += 6
            reasons.append(f"{cat_name}:domain_rx")
        elif r["re_weak"] and any(r["re_weak"].search(v) for v in variants):
            pts += 2
            reasons.append(f"{cat_name}:domain_weak")

        if clean_ctx:
            if r["re_ctx_rx"] and r["re_ctx_rx"].search(clean_ctx):
                pts += 4
                reasons.append(f"{cat_name}:ctx_rx")
            if r["re_weak"] and r["re_weak"].search(clean_ctx):
                pts += 2
                reasons.append(f"{cat_name}:ctx_weak")

        if pts > best_score:
            best_cat, best_score, best_reasons = cat_name, pts, reasons

    return best_cat, best_score, best_reasons
