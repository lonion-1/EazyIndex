# Copyright (c) 2026 EazyWhale
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pemanen saran kata kunci (multi-provider autocomplete) -> langsung masuk ke feed_cache.py."""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from ..config_loader import IndexerConfig
from ..text_utils import norm_space
from .feed_cache import FEED_FILE, load_feed, push_candidates

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

RE_THAI = re.compile(r"[\u0E00-\u0E7F]")
RE_VIET = re.compile(
    r"[ăâđêôơưàảãáạằẳẵắặầẩẫấậèẻẽéẹềểễếệìỉĩíịòỏõóọồổỗốộờởỡớợùủũúụừửữứựỳỷỹýỵ]",
    re.IGNORECASE,
)
RE_JP = re.compile(r"[\u3040-\u30FF\u4E00-\u9FAF]")
RE_KR = re.compile(r"[\uAC00-\uD7AF]")
RE_JSONP = re.compile(r"^[a-zA-Z0-9_$.]+\((.*)\)\s*;?\s*$", re.DOTALL)

DDG_KL_MAP: dict[tuple[str, str], str] = {
    ("id", "id"): "id-id",
    ("ms", "my"): "my-ms",
    ("en", "ph"): "ph-en",
    ("th", "th"): "th-th",
    ("vi", "vn"): "vn-vi",
    ("ja", "jp"): "jp-jp",
    ("ko", "kr"): "kr-kr",
}

MODIFIERS_BY_CAT: dict[str, tuple[str, ...]] = {
    "judol": ("link", "login", "daftar", "alternatif", "terbaru", "{year}"),
    "film": ("nonton", "streaming", "sub indo", "terbaru", "{year}"),
    "comic": ("baca", "komik", "sub indo", "chapter terbaru", "{year}"),
    "porn": ("link", "terbaru", "sub indo", "{year}"),
    "phishing": ("link", "resmi", "klaim", "{year}"),
}


class ProviderBlockedError(Exception):
    """Provider membalas HTTP 403/429."""


def _pick_locale(seed: str) -> tuple[str, str]:
    if RE_THAI.search(seed):
        return ("th", "th")
    if RE_VIET.search(seed):
        return ("vi", "vn")
    if RE_JP.search(seed):
        return ("ja", "jp")
    if RE_KR.search(seed):
        return ("ko", "kr")
    return random.choice([("id", "id"), ("id", "id"), ("ms", "my")])


def _build_probe_query(seed: str, cat: str, year: str) -> str:
    r = random.random()
    if r < 0.55:
        return seed
    if r < 0.80:
        return f"{seed} {year}"
    mods = MODIFIERS_BY_CAT.get(cat, ("terbaru", "{year}"))
    mod = random.choice(mods).replace("{year}", year)
    if mod in seed.lower():
        return seed
    return (
        f"{mod} {seed}"
        if mod in ("link", "login", "daftar", "nonton", "baca")
        else f"{seed} {mod}"
    )


def _choose_source(gl: str, disabled: set[str]) -> str | None:
    if gl == "jp":
        candidates = [("yahoo_jp", 0.50), ("youtube", 0.30), ("google", 0.20)]
    else:
        candidates = [("google", 0.40), ("youtube", 0.35), ("ddg", 0.25)]
    active = [(src, w) for src, w in candidates if src not in disabled]
    if not active:
        return None
    sources, weights = zip(*active)
    return random.choices(sources, weights=weights, k=1)[0]


def fetch_suggestions(q_query: str, hl: str, gl: str, source: str) -> list[str]:
    encoded_q = urllib.parse.quote(q_query)
    if source == "youtube":
        url = f"https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&hl={hl}&gl={gl}&q={encoded_q}"
        referer = "https://www.youtube.com/"
    elif source == "ddg":
        kl = DDG_KL_MAP.get((hl, gl), "id-id")
        url = f"https://duckduckgo.com/ac/?q={encoded_q}&kl={kl}"
        referer = "https://duckduckgo.com/"
    elif source == "yahoo_jp":
        url = f"https://ff.search.yahoo.co.jp/gossip?output=json&command={encoded_q}"
        referer = "https://search.yahoo.co.jp/"
    else:
        url = f"https://suggestqueries.google.com/complete/search?client=firefox&hl={hl}&gl={gl}&q={encoded_q}"
        referer = "https://www.google.com/"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json,text/javascript,*/*;q=0.8",
            "Accept-Language": f"{hl}-{gl.upper()},{hl};q=0.9,en;q=0.7",
            "Referer": referer,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            cs = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read(250_000).decode(cs, errors="ignore").strip()
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise ProviderBlockedError(f"HTTP {e.code}") from e
        raise

    m_jsonp = RE_JSONP.match(raw)
    if m_jsonp:
        raw = m_jsonp.group(1)

    data = json.loads(raw)
    if source in ("google", "youtube"):
        if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list):
            return [s for s in data[1] if isinstance(s, str)]
    elif source == "ddg":
        if isinstance(data, list):
            return [
                item["phrase"]
                for item in data
                if isinstance(item, dict) and isinstance(item.get("phrase"), str)
            ]
    elif source == "yahoo_jp":
        if isinstance(data, dict) and isinstance(data.get("r"), list):
            return [
                item["k"]
                for item in data["r"]
                if isinstance(item, dict) and isinstance(item.get("k"), str)
            ]
    return []


def harvest_autocomplete(
    cfg: IndexerConfig,
    max_req: int = 30,
    cats: set[str] | None = None,
    dry: bool = False,
) -> dict:
    """Panen saran autocomplete dan setorkan langsung ke feed_cache.py."""
    req_count = 0
    raw_candidates: list[tuple[str, str, str]] = []
    hits_by_src: dict[str, int] = {}
    disabled: set[str] = set()
    err_by_src: dict[str, int] = {}
    year = str(time.gmtime().tm_year)

    existing_feed = load_feed(FEED_FILE)
    cat_names = [c for c in cfg.categories if not cats or c in cats]
    random.shuffle(cat_names)
    per_cat_quota = max(2, max_req // max(1, len(cat_names)))

    for cat in cat_names:
        if req_count >= max_req:
            break
        q_cfg = cfg.categories[cat].queries_cfg
        base_seeds = list(
            dict.fromkeys(
                s.strip()
                for s in (
                    list(q_cfg.get("fillers", [])) + list(q_cfg.get("brands", []))
                )
                if s and s.strip()
            )
        )
        chained = [
            q
            for (rcat, q), row in existing_feed.items()
            if rcat == cat and row.get("hits", 0) > 0
        ]
        seed_pool = list(
            dict.fromkeys(base_seeds + random.sample(chained, k=min(2, len(chained))))
        )
        if not seed_pool:
            continue

        for seed in random.sample(seed_pool, k=min(per_cat_quota, len(seed_pool))):
            if req_count >= max_req:
                break
            hl, gl = _pick_locale(seed)
            source = _choose_source(gl, disabled)
            if source is None:
                req_count = max_req
                break

            q_query = _build_probe_query(seed, cat, year)
            req_count += 1
            try:
                suggestions = fetch_suggestions(q_query, hl, gl, source)
                err_by_src[source] = 0
                for s in suggestions:
                    clean = norm_space(s).lower()
                    if 4 <= len(clean) <= 72 and not clean.startswith(
                        ("http://", "https://")
                    ):
                        raw_candidates.append((clean, cat, f"suggest:{source}"))
                        hits_by_src[source] = hits_by_src.get(source, 0) + 1
            except ProviderBlockedError:
                disabled.add(source)
            except Exception:
                err_by_src[source] = err_by_src.get(source, 0) + 1
                if err_by_src[source] >= 3:
                    disabled.add(source)
            time.sleep(random.uniform(0.8, 1.6))

    accepted = push_candidates(cfg, raw_candidates, dry=dry)
    total_pool = len(load_feed(FEED_FILE))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(
                    f"## 🔍 Suggest Harvester{' (dry-run)' if dry else ''}\n"
                    f"- Request: {req_count} | Diterima ke feed: +{accepted} | Total pool: {total_pool}\n\n"
                )
        except OSError:
            pass

    return {
        "requests": req_count,
        "accepted": accepted,
        "pool": total_pool,
        "sources": hits_by_src,
    }
