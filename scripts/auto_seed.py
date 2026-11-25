# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pemanen saran kata kunci berbasis Web Autocomplete (konfigurasi penuh via config/auto_seed.toml)."""

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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from indexer.config_loader import (
    ConfigError,
    _compile_literals,
    _load_toml,
    load_indexer_config,
)
from indexer.text_utils import deobfuscate

SUGGEST_FILE = ROOT / ".cache" / "webindex" / "suggest.json"

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


class ProviderBlockedError(Exception):
    """Dilempar saat provider membalas HTTP 403/429 agar hanya provider tersebut yang dimatikan."""


def log(tag: str, msg: str) -> None:
    ts = time.strftime("%H:%M:%S", time.gmtime())
    print(f"[{ts}] [{tag}] {msg}", flush=True)


def norm_q(text: str) -> str:
    return " ".join(html.unescape(text).split()).strip().lower()


def load_seed_settings(cdir: Path) -> dict:
    toml_path = cdir / "auto_seed.toml"
    raw: dict = {}
    if toml_path.exists():
        try:
            raw = _load_toml(toml_path)
        except ConfigError as e:
            log("SUGGEST", f"Peringatan auto_seed.toml ({e}) — memakai default")

    gen = raw.get("general") or {}
    src = raw.get("sources") or {}
    prb = raw.get("probe") or {}
    flt = raw.get("filters") or {}
    mods = raw.get("modifiers") or {}

    noise_words = [w for w in flt.get("noise_words", []) if isinstance(w, str)]
    re_noise_words = _compile_literals(noise_words, word_boundary=True)

    noise_rx_list = []
    for pat in flt.get("noise_regexes", [r"[?!]"]):
        if isinstance(pat, str) and pat.strip():
            try:
                noise_rx_list.append(re.compile(pat.strip(), re.IGNORECASE))
            except re.error as e:
                log("SUGGEST", f"Regex noise diabaikan ({pat!r}): {e}")

    portal_kw = [w for w in flt.get("portal_keywords", []) if isinstance(w, str)]
    re_portal_kw = _compile_literals(portal_kw, word_boundary=True)

    tlds = [
        re.escape(t.strip().lower().lstrip("."))
        for t in flt.get("portal_tlds", [])
        if isinstance(t, str) and t.strip()
    ]
    re_portal_dom = (
        re.compile(rf"\b[a-z0-9-]{{2,}}\.(?:{'|'.join(tlds)})\b", re.IGNORECASE)
        if tlds
        else None
    )

    return {
        "max_per_cat": int(gen.get("max_per_cat", 600)),
        "min_words": int(gen.get("min_words", 1)),
        "max_words": int(gen.get("max_words", 5)),
        "min_len": int(gen.get("min_len", 4)),
        "max_len": int(gen.get("max_len", 48)),
        "sleep_min": float(gen.get("sleep_min", 1.2)),
        "sleep_max": float(gen.get("sleep_max", 2.5)),
        "require_portal_or_brand": bool(gen.get("require_portal_or_brand", True)),
        "enabled_sources": list(src.get("enabled", ["google", "ddg"])),
        "weights": dict(src.get("weights") or {"google": 0.55, "ddg": 0.45}),
        "jp_weights": dict(
            src.get("jp_weights") or {"yahoo_jp": 0.50, "google": 0.30, "ddg": 0.20}
        ),
        "brand_plain_prob": float(prb.get("brand_plain_prob", 0.45)),
        "year_prob": float(prb.get("year_prob", 0.25)),
        "prefix_modifiers": {
            m.strip().lower()
            for m in prb.get(
                "prefix_modifiers", ["link", "login", "daftar", "situs", "web"]
            )
            if isinstance(m, str)
        },
        "default_modifiers": list(
            prb.get("default_modifiers", ["situs", "link", "alternatif", "{year}"])
        ),
        "mods_by_cat": dict(mods.get("by_category") or {}),
        "mods_by_locale": dict(mods.get("by_locale") or {}),
        "re_noise_words": re_noise_words,
        "noise_regexes": noise_rx_list,
        "re_portal_kw": re_portal_kw,
        "re_portal_dom": re_portal_dom,
    }


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


def build_osint_probe(
    seed: str, is_brand: bool, cat: str, year: str, gl: str, settings: dict
) -> str:
    r = random.random()
    plain_cut = settings["brand_plain_prob"]
    year_cut = plain_cut + settings["year_prob"]

    if is_brand and r < plain_cut:
        return seed
    if r < year_cut:
        return f"{seed} {year}"

    loc_mods = settings["mods_by_locale"].get(gl)
    cat_mods = settings["mods_by_cat"].get(cat) or settings["default_modifiers"]
    mods = loc_mods if loc_mods else cat_mods
    if not mods:
        return seed

    mod = str(random.choice(mods)).replace("{year}", year).strip()
    if not mod or mod.lower() in seed.lower():
        return seed
    if loc_mods:
        return f"{seed} {mod}"
    if mod.lower() in settings["prefix_modifiers"]:
        return f"{mod} {seed}"
    return f"{seed} {mod}"


def choose_web_source(
    gl: str, disabled: set[str], settings: dict
) -> str | None:
    enabled = set(settings["enabled_sources"])
    weight_map = settings["jp_weights"] if gl == "jp" else settings["weights"]
    active = [
        (src, float(w))
        for src, w in weight_map.items()
        if src in enabled and src not in disabled and float(w) > 0
    ]
    if not active:
        fallback = [s for s in enabled if s not in disabled]
        return random.choice(fallback) if fallback else None

    sources, weights = zip(*active)
    return random.choices(sources, weights=weights, k=1)[0]


def fetch_web_suggestions(q_query: str, hl: str, gl: str, source: str) -> list[str]:
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


def is_valid_osint_query(
    clean: str,
    variants: list[str],
    cat: str,
    cat_rule,
    cfg,
    settings: dict,
) -> bool:
    words = clean.split()
    if not (settings["min_words"] <= len(words) <= settings["max_words"]):
        return False
    if not (settings["min_len"] <= len(clean) <= settings["max_len"]):
        return False
    if clean.startswith(("http://", "https://")):
        return False

    re_nw = settings["re_noise_words"]
    if re_nw is not None and any(re_nw.search(v) for v in variants):
        return False
    if any(rx.search(v) for rx in settings["noise_regexes"] for v in variants):
        return False

    neg_patterns = [
        rx
        for rx in (cat_rule.re_passive_context, cfg.legal.re_news_reporting)
        if rx is not None
    ]
    if any(rx.search(v) for rx in neg_patterns for v in variants):
        return False

    has_brand_or_hint = (
        cat_rule.re_domain_hints is not None
        and any(cat_rule.re_domain_hints.search(v) for v in variants)
    ) or (
        cat == "judol"
        and cfg.re_any_illegal_domain is not None
        and any(cfg.re_any_illegal_domain.search(v) for v in variants)
    )

    re_pkw = settings["re_portal_kw"]
    re_pdom = settings["re_portal_dom"]
    has_portal_signal = (
        (re_pkw is not None and any(re_pkw.search(v) for v in variants))
        or (re_pdom is not None and any(re_pdom.search(v) for v in variants))
    )

    if settings["require_portal_or_brand"] and not (
        has_brand_or_hint or has_portal_signal
    ):
        return False

    if not has_brand_or_hint and cat_rule.re_active_intent is not None:
        if not any(cat_rule.re_active_intent.search(v) for v in variants):
            return False

    return True


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
        "## 🔍 Web OSINT Suggest Harvester" + (" (dry-run)" if dry else ""),
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
        description="Harvest Web Autocomplete queries for OSINT domain discovery"
    )
    ap.add_argument(
        "--max-req", type=int, default=35, help="batas maksimal request per sesi"
    )
    ap.add_argument(
        "--config-dir", type=str, default="", help="direktori konfigurasi TOML"
    )
    ap.add_argument("--dry-run", action="store_true", help="jalan tanpa menulis file")
    args = ap.parse_args(argv)

    cdir = Path(args.config_dir) if args.config_dir else (ROOT / "config")
    try:
        cfg = load_indexer_config(cdir)
    except ConfigError as e:
        log("SUGGEST", f"Konfigurasi tidak tersedia ({e}) — dilewati")
        return 0

    settings = load_seed_settings(cdir)
    max_per_cat = settings["max_per_cat"]

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

    cat_names = [
        c for c, rule in cfg.categories.items() if rule.enabled and rule.queries_cfg
    ]
    random.shuffle(cat_names)
    per_cat_quota = max(3, args.max_req // max(1, len(cat_names)))

    for cat in cat_names:
        if req_count >= args.max_req:
            break

        cat_rule = cfg.categories[cat]
        q_cfg = cat_rule.queries_cfg

        existing_list = items.setdefault(cat, [])
        existing = set(existing_list)
        known_plain = {
            norm_q(q.replace("{year}", year))
            for q in q_cfg.get("plain", [])
            if isinstance(q, str)
        }

        brands = {
            s.strip().lower()
            for s in q_cfg.get("brands", [])
            if isinstance(s, str) and s.strip()
        }
        fillers = [
            s.strip()
            for s in q_cfg.get("fillers", [])
            if isinstance(s, str) and s.strip()
        ]
        chained = (
            random.sample(existing_list, k=min(2, len(existing_list)))
            if existing_list
            else []
        )

        seed_pool = list(dict.fromkeys(list(brands) + chained + fillers))
        if not seed_pool:
            continue

        sample_seeds = random.sample(seed_pool, k=min(per_cat_quota, len(seed_pool)))
        for seed in sample_seeds:
            if req_count >= args.max_req:
                break

            hl, gl = pick_locale(seed)
            source = choose_web_source(gl, disabled_sources, settings)
            if source is None:
                log(
                    "SUGGEST",
                    "semua provider web terkena rate-limit/error → berhenti aman",
                )
                req_count = args.max_req
                break

            is_brand = seed.strip().lower() in brands
            q_query = build_osint_probe(seed, is_brand, cat, year, gl, settings)
            req_count += 1

            try:
                suggestions = fetch_web_suggestions(q_query, hl, gl, source)
                err_by_src[source] = 0
                for s in suggestions:
                    clean = norm_q(s)
                    if clean in existing or clean in known_plain:
                        continue
                    variants = list(dict.fromkeys([clean] + deobfuscate(clean)))
                    if is_valid_osint_query(
                        clean, variants, cat, cat_rule, cfg, settings
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

            time.sleep(random.uniform(settings["sleep_min"], settings["sleep_max"]))

    for cat in items:
        items[cat] = items[cat][-max_per_cat:]

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
        for p in (SUGGEST_FILE.parent.parent, SUGGEST_FILE.parent):
            if p.exists() and not p.is_dir():
                p.unlink()
        SUGGEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SUGGEST_FILE.with_name("suggest.json.tmp")
        tmp.write_text(
            json.dumps({"items": items}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, SUGGEST_FILE)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
