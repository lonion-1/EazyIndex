# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Upstream Intersection & Domain-Hint Learner (Tahap Akhir).
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE_DIR = ROOT / "pipeline"
CURATED_DIR = ROOT / "Curated"
ALLOWLIST_FILE = ROOT / "Allowlist.txt"
BENIGN_POOL_FILE = ROOT / "_vault" / "benign_pool.txt"
HINTS_OUT_FILE = PIPELINE_DIR / "learned_domain_hints.jsonl"

STOPWORDS = frozenset(
    {
        "www",
        "com",
        "net",
        "org",
        "xyz",
        "info",
        "site",
        "online",
        "top",
        "vip",
        "pro",
        "club",
        "shop",
        "store",
        "web",
        "app",
        "link",
        "live",
        "id",
        "co",
        "go",
        "ac",
        "or",
        "sch",
        "mil",
        "my",
        "sg",
        "uk",
        "us",
        "jp",
        "kr",
        "cn",
        "asia",
        "click",
        "login",
        "daftar",
        "resmi",
        "baru",
        "terbaru",
        "alternatif",
        "slot",
        "gacor",
    }
)

# Contoh sumber list besar publik (bisa ditambah/diatur di config/fp_auditor.toml)
UPSTREAM_FEEDS = [
    (
        "hagezi-gambling",
        "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/gambling.txt",
    ),
    (
        "hagezi-tif",
        "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/tif.txt",
    ),
]


def _load_safe_tokens() -> tuple[set[str], set[str]]:
    safe_domains: set[str] = set()
    safe_tokens: set[str] = set(STOPWORDS)
    for p in (ALLOWLIST_FILE, BENIGN_POOL_FILE):
        if not p.exists():
            continue
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = (
                ln.split("#", 1)[0]
                .strip()
                .lower()
                .removeprefix("@@||")
                .removeprefix("||")
                .removesuffix("^")
            )
            if not s or s.startswith("!"):
                continue
            safe_domains.add(s)
            for lab in s.split(".")[:-1]:
                for tok in re.split(r"[-0-9_]+", lab):
                    if len(tok) >= 4:
                        safe_tokens.add(tok)
    return safe_domains, safe_tokens


def _extract_tokens(domain: str) -> list[str]:
    tokens: list[str] = []
    for lab in domain.split(".")[:-1]:
        for tok in re.split(r"[-0-9_]+", lab):
            if 4 <= len(tok) <= 18 and tok.isalpha() and tok not in STOPWORDS:
                tokens.append(tok)
    return list(dict.fromkeys(tokens))


def _fetch_upstream_matches(url: str, our_domains: set[str]) -> set[str]:
    """Streaming baris demi baris agar hemat RAM; hanya mengembalikan irisan dengan our_domains."""
    matched: set[str] = set()
    req = urllib.request.Request(
        url, headers={"User-Agent": "EazyWeb-IntersectionLearner/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            for raw_line in resp:
                s = raw_line.decode("utf-8", errors="ignore").strip().lower()
                if not s or s[0] in ("!", "#", "[", "@"):
                    continue
                if s.startswith("||") and "^" in s:
                    dom = s[2:].split("^", 1)[0]
                else:
                    dom = s.split()[0]
                if dom in our_domains:
                    matched.add(dom)
    except Exception as e:
        print(f"[UPSTREAM-PEEK] Gagal baca {url}: {e}")
    return matched


def peek_and_learn(
    records: list[dict], feeds: list[tuple[str, str]] | None = None
) -> list[dict]:
    """Perkaya record .jsonl dengan irisan list besar & simpan hint domain baru."""
    if not records:
        return records

    feeds = feeds or UPSTREAM_FEEDS
    safe_domains, safe_tokens = _load_safe_tokens()
    our_domains = {r["d"].lower() for r in records if r.get("d")}

    # 1. Cari irisan (hanya domain kita yang tertangkap juga di list besar)
    hits_by_domain: dict[str, list[str]] = {}
    for feed_name, feed_url in feeds:
        matched = _fetch_upstream_matches(feed_url, our_domains)
        print(
            f"[UPSTREAM-PEEK] {feed_name}: {len(matched)} temuan kita ikut tertangkap di upstream"
        )
        for dom in matched:
            hits_by_domain.setdefault(dom, []).append(feed_name)

    # 2. Tambah skor & catatan kaki (trail) untuk domain yang beririsan, lalu panen token hint
    token_counter_by_cat: dict[str, Counter[str]] = {}
    for rec in records:
        dom = rec["d"].lower()
        cat = rec.get("cat", "judol")
        up_hits = hits_by_domain.get(dom, [])
        if up_hits:
            rec["s"] = int(rec.get("s", 0)) + (2 * len(up_hits))
            rec["upstream_hit"] = sorted(
                set(rec.get("upstream_hit", [])) | set(up_hits)
            )
            rec.setdefault("trail", []).append(
                {
                    "step": "upstream-peek",
                    "note": f"beririsan dengan {', '.join(up_hits)} (+{2 * len(up_hits)} skor)",
                }
            )
            # Kumpulkan token domain dari temuan terkonfirmasi untuk dipelajari sebagai hint baru
            toks = [t for t in _extract_tokens(dom) if t not in safe_tokens]
            if toks:
                rec["hints"] = toks
                token_counter_by_cat.setdefault(cat, Counter()).update(toks)

    # 3. Simpan hint baru yang muncul >= 2x & lulus cek 0 FP terhadap safe_domains
    learned_rows: list[dict] = []
    for cat, cnt in sorted(token_counter_by_cat.items()):
        for tok, freq in cnt.most_common(40):
            if freq < 2:
                continue
            rx = re.compile(rf"(?<!\w){re.escape(tok)}(?!\w)", re.IGNORECASE)
            if any(rx.search(sd) for sd in safe_domains):
                continue
            learned_rows.append(
                {
                    "cat": cat,
                    "hint": tok,
                    "freq": freq,
                    "source": "upstream_intersection",
                }
            )

    if learned_rows:
        HINTS_OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HINTS_OUT_FILE.with_name(HINTS_OUT_FILE.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for row in learned_rows:
                f.write(
                    json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
        os.replace(tmp, HINTS_OUT_FILE)
        print(
            f"[UPSTREAM-PEEK] +{len(learned_rows)} kandidat domain_hints baru disimpan ke {HINTS_OUT_FILE.name}"
        )

    return records
