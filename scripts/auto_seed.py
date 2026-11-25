# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pemanen saran kata kunci (multi-provider autocomplete) untuk memperkaya pool query Indexer."""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from radar.core import ROOT, deobfuscate, load_config, log

SUGGEST_FILE = ROOT / ".cache" / "webindex" / "suggest.json"
MAX_PER_CAT = 600

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
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
    "anime": ("nonton", "sub indo", "episode terbaru", "{year}"),
    "adult": ("link", "terbaru", "bokep", "{year}"),
}

NATIVE_MODIFIERS: dict[str, tuple[str, ...]] = {
    "jp": ("無料", "最新", "raw", "{year}"),
    "th": ("ซับไทย", "ออนไลน์", "ล่าสุด", "{year}"),
    "vn": ("vietsub", "mới nhất", "link", "{year}"),
    "kr": ("무료", "최신", "다시보기", "{year}"),
}


class ProviderBlockedError(Exception):
    """Dilempar saat provider membalas HTTP 403/429 agar hanya provider tersebut yang dimatikan."""


def norm_q(text: str) -> str:
    return " ".join(html.unescape(text).split()).strip().lower()


def pick_locale(seed: str) -> tuple[str, str]:
    if RE_THAI.search(seed):
        return ("th", "th")
    if RE_VIET.search(seed):
        return ("vi", "vn")
    if RE_JP.search(seed):
        return ("ja", "jp")
    if RE_KR.search(seed):
        return ("ko", "kr")
    return random.choice([("id", "id"), ("id", "id"), ("ms", "my"), ("en", "ph")])


def build_probe_query(seed: str, cat: str, year: str, gl: str) -> str:
    r = random.random()
    if r < 0.55:
        return seed
    if r < 0.80:
        return f"{seed} {year}"

    mods = NATIVE_MODIFIERS.get(gl) or MODIFIERS_BY_CAT.get(cat, ("terbaru", "{year}"))
    mod = random.choice(mods).replace("{year}", year)
    if mod in seed.lower():
        return seed
    if gl in NATIVE_MODIFIERS:
        return f"{seed} {mod}"
    return (
        f"{mod} {seed}"
        if mod in ("link", "login", "daftar", "nonton", "baca")
        else f"{seed} {mod}"
    )


def choose_source(gl: str, disabled: set[str]) -> str | None:
    if gl == "jp":
        candidates = [("yahoo_jp", 0.50), ("youtube", 0.30), ("google", 0.20)]
    else:
        candidates = [("google", 0.40), ("youtube", 0.35), ("ddg", 0.25)]

    active = [(src, w) for src, w in candidates if src not in disabled]
    if not active:
        fallback = [
            src
            for src in ("ddg", "youtube", "google", "yahoo_jp")
            if src not in disabled
        ]
        return random.choice(fallback) if fallback else None

    sources, weights = zip(*active)
    return random.choices(sources, weights=weights, k=1)[0]


def fetch_suggestions(q_query: str, hl: str, gl: str, source: str) -> list[str]:
    encoded_q = urllib.parse.quote(q_query)

    if source == "youtube":
        url = f"https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&hl={hl}&gl={gl}&q={encoded_q}"
        referer = "https://www.youtube.com/"
    elif source == "ddg":
        kl = DDG_KL_MAP.get((hl, gl), "wt-wt")
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

    # Antisipasi jika endpoint mengembalikan format JSONP callback(...)
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
        if isinstance(data, dict):
            if isinstance(data.get("r"), list):
                return [
                    item["k"]
                    for item in data["r"]
                    if isinstance(item, dict) and isinstance(item.get("k"), str)
                ]
            gossip = data.get("gossip")
            if isinstance(gossip, dict) and isinstance(gossip.get("k"), list):
                return [k for k in gossip["k"] if isinstance(k, str)]

    return []


def tulis_ringkasan(
    req_count: int,
    added: dict[str, list[str]],
    hits_by_src: dict[str, int],
    disabled: set[str],
    total_pool: int,
    dry: bool,
) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    total_new = sum(len(v) for v in added.values())
    src_stat = ", ".join(f"{k}: +{v}" for k, v in sorted(hits_by_src.items())) or "-"
    blocked_note = f" | **Blocked:** {', '.join(sorted(disabled))}" if disabled else ""
    lines = [
        "## 🔍 Multi-Source Suggest Harvester" + (" (dry-run)" if dry else ""),
        "",
        f"- **Request:** {req_count} | **Baru:** +{total_new} | **Total kolam:** {total_pool}{blocked_note}",
        f"- **Per Sumber:** {src_stat}",
        "",
    ]
    for cat, qs in sorted(added.items()):
        if qs:
            lines.append(
                f"**[{cat}] (+{len(qs)}):** " + ", ".join(f"`{q}`" for q in qs[:30])
            )
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Harvest Multi-Source Autocomplete queries"
    )
    ap.add_argument(
        "--max-req", type=int, default=35, help="batas maksimal request per sesi"
    )
    ap.add_argument("--dry-run", action="store_true", help="jalan tanpa menulis file")
    args = ap.parse_args(argv)

    cfg = load_config()
    if cfg.classifier is None:
        log("SUGGEST", "config/index.toml tidak ada — dilewati")
        return 0

    icfg = cfg.index or {}
    queries_cfg = icfg.get("queries") or {}

    items: dict[str, list[str]] = {}
    if SUGGEST_FILE.exists():
        try:
            raw_items = json.loads(SUGGEST_FILE.read_text(encoding="utf-8")).get(
                "items", {}
            )
            for c, qs in raw_items.items():
                if isinstance(qs, list):
                    items[c] = list(
                        dict.fromkeys(
                            norm_q(q) for q in qs if isinstance(q, str) and norm_q(q)
                        )
                    )
        except (ValueError, OSError):
            items = {}

    req_count = 0
    added_by_cat: dict[str, list[str]] = {}
    hits_by_src: dict[str, int] = {}
    disabled_sources: set[str] = set()
    err_by_src: dict[str, int] = {}
    year = str(time.gmtime().tm_year)

    cat_names = [c for c in queries_cfg if c != "mirror" and c in cfg.classifier.cats]
    random.shuffle(cat_names)
    per_cat_quota = max(3, args.max_req // max(1, len(cat_names)))

    for cat in cat_names:
        if req_count >= args.max_req:
            break

        cat_cls = cfg.classifier.cats[cat]
        q_cfg = queries_cfg[cat]

        existing_list = items.setdefault(cat, [])
        existing = set(existing_list)
        known_plain = {
            norm_q(q.replace("{year}", year)) for q in q_cfg.get("plain", [])
        }

        base_seeds = list(
            dict.fromkeys(
                s.strip()
                for s in (
                    list(q_cfg.get("fillers", [])) + list(q_cfg.get("brands", []))
                )
                if s and s.strip()
            )
        )
        chained_seeds = (
            random.sample(existing_list, k=min(2, len(existing_list)))
            if existing_list
            else []
        )
        seed_pool = list(dict.fromkeys(base_seeds + chained_seeds))
        if not seed_pool:
            continue

        sample_seeds = random.sample(seed_pool, k=min(per_cat_quota, len(seed_pool)))
        for seed in sample_seeds:
            if req_count >= args.max_req:
                break

            hl, gl = pick_locale(seed)
            source = choose_source(gl, disabled_sources)
            if source is None:
                log(
                    "SUGGEST", "semua provider terkena rate-limit/error → berhenti aman"
                )
                req_count = args.max_req
                break

            q_query = build_probe_query(seed, cat, year, gl)
            req_count += 1

            try:
                suggestions = fetch_suggestions(q_query, hl, gl, source)
                err_by_src[source] = 0
                for s in suggestions:
                    clean = norm_q(s)
                    if not (4 <= len(clean) <= 80) or clean.startswith(
                        ("http://", "https://")
                    ):
                        continue
                    variants = [clean] + deobfuscate(clean)
                    is_hit = any(cat_cls.re_strong.search(v) for v in variants) or (
                        cat == "judol"
                        and any(cfg.re_strong.search(v) for v in variants)
                    )
                    if (
                        clean not in existing
                        and clean not in known_plain
                        and is_hit
                        and not any(cat_cls.re_neg.search(v) for v in variants)
                    ):
                        existing.add(clean)
                        existing_list.append(clean)
                        added_by_cat.setdefault(cat, []).append(clean)
                        hits_by_src[source] = hits_by_src.get(source, 0) + 1
                        log("SUGGEST", f"  + [{cat}|{source}] {clean!r}")
            except ProviderBlockedError as e:
                disabled_sources.add(source)
                log(
                    "SUGGEST",
                    f"provider {source} kena rate-limit ({e}) → dialihkan ke provider lain",
                )
            except Exception as e:
                err_by_src[source] = err_by_src.get(source, 0) + 1
                log("SUGGEST", f"gagal fetch [{source}] {q_query!r}: {e}")
                if err_by_src[source] >= 3:
                    disabled_sources.add(source)
                    log(
                        "SUGGEST",
                        f"provider {source} dinonaktifkan sementara (3x error beruntun)",
                    )

            time.sleep(random.uniform(1.2, 2.5))

    for cat in items:
        items[cat] = items[cat][-MAX_PER_CAT:]

    added_total = sum(len(v) for v in added_by_cat.values())
    total_pool = sum(len(v) for v in items.values())
    log(
        "SUGGEST",
        f"Selesai: {req_count} req | +{added_total} query baru | total pool={total_pool} | src={hits_by_src}",
    )
    tulis_ringkasan(
        req_count, added_by_cat, hits_by_src, disabled_sources, total_pool, args.dry_run
    )

    if not args.dry_run:
        SUGGEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SUGGEST_FILE.with_name("suggest.json.tmp")
        tmp.write_text(
            json.dumps({"items": items}, ensure_ascii=False, indent=0) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, SUGGEST_FILE)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
