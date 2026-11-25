# Copyright (c) 2026 EazyWhale
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dynamic Self-Pruning Query Feed Cache (.cache/webindex/feed.jsonl).
Terhubung penuh dengan auto_seed, queries, dan main indexer loop:
- Query yang menghasilkan kandidat lolos gatekeeper -> hits naik & prioritas meningkat.
- Query yang zonk atau hanya menghasilkan situs legal ter-veto -> misses naik & terhapus permanen.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path

from ..config_loader import ROOT, IndexerConfig
from ..text_utils import deobfuscate, norm_space

FEED_FILE = ROOT / ".cache" / "webindex" / "feed.jsonl"

DAY = 86400
MAX_FEED_BYTES = 64 * 1024
MAX_AGE_DAYS = 14
MIN_KEEP_SCORE = 2.0
MAX_ZONK_NEW = 2
MAX_ZONK_PROVEN = 3
MIN_REUSE_HOURS = 6

RE_LOOKS_LIKE_DOMAIN = re.compile(r"^(?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/.*)?$", re.I)
RE_INTENT_BOOST = re.compile(
    r"\b(?:login|daftar|link|alternatif|rtp|gacor|maxwin|slot|togel|sub\s*indo|nonton|baca|stream|download|terbaru|gratis|vip)\b",
    re.I,
)


def _norm_q(q: str) -> str:
    return norm_space(q).lower()


def _token_signature(q: str) -> str:
    toks = sorted(set(re.findall(r"[a-z0-9\u0E00-\u0E7F\u3040-\u30FF\u4E00-\u9FAF\uAC00-\uD7AF]+", q)))
    return " ".join(toks) if toks else q


def score_query(cfg: IndexerConfig, q: str, cat: str, src: str) -> int:
    """Evaluasi kelayakan query berdasarkan aturan kategori & kontra-bukti berita legal."""
    if not q or not (4 <= len(q) <= 72):
        return 0
    if q.isdigit() or RE_LOOKS_LIKE_DOMAIN.match(q):
        return 0

    variants = deobfuscate(q, 200)
    if cfg.legal.re_news_reporting and any(cfg.legal.re_news_reporting.search(v) for v in variants):
        return 0

    rule = cfg.categories.get(cat)
    if rule is None:
        return 0

    if rule.re_passive_context and any(rule.re_passive_context.search(v) for v in variants):
        return 0

    pts = 1 if "suggest" in src else 2

    if rule.re_active_intent and any(rule.re_active_intent.search(v) for v in variants):
        pts += 5
    if rule.re_domain_hints and any(rule.re_domain_hints.search(v) for v in variants):
        pts += 3
    if RE_INTENT_BOOST.search(q):
        pts += 2

    if pts <= 2:
        return 0
    return pts


def _calc_effective_score(row: dict, ts: int) -> float:
    age_days = max(0.0, (ts - int(row.get("ts", ts))) / DAY)
    base_s = float(row.get("s", 0))
    hits = int(row.get("hits", 0))
    misses = int(row.get("misses", 0))
    return base_s + (hits * 2.5) - (misses * 1.5) - (age_days * 0.6)


def _should_evict(row: dict, ts: int, cfg: IndexerConfig | None = None) -> bool:
    q = row.get("q", "")
    cat = row.get("cat", "")
    src = row.get("src", "unknown")

    if not q or RE_LOOKS_LIKE_DOMAIN.match(q):
        return True
    if cfg is not None and score_query(cfg, q, cat, src) <= 0:
        return True

    hits = int(row.get("hits", 0))
    misses = int(row.get("misses", 0))
    if hits == 0 and misses >= MAX_ZONK_NEW:
        return True
    if misses >= MAX_ZONK_PROVEN:
        return True

    total_runs = hits + misses
    if total_runs >= 5 and (hits / total_runs) < 0.15:
        return True

    age_days = max(0.0, (ts - int(row.get("ts", ts))) / DAY)
    if age_days > MAX_AGE_DAYS:
        return True

    return _calc_effective_score(row, ts) < MIN_KEEP_SCORE


def load_feed(path: Path = FEED_FILE) -> dict[tuple[str, str], dict]:
    records: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return records
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if not s or not s.startswith("{"):
                    continue
                try:
                    row = json.loads(s)
                except ValueError:
                    continue
                q = _norm_q(str(row.get("q", "")))
                cat = str(row.get("cat", "")).strip().lower()
                if q and cat:
                    records[(cat, q)] = {
                        "q": q,
                        "cat": cat,
                        "s": int(row.get("s", 1)),
                        "src": str(row.get("src", "unknown")),
                        "ts": int(row.get("ts", 0)),
                        "used": int(row.get("used", 0)),
                        "hits": int(row.get("hits", 0)),
                        "misses": int(row.get("misses", 0)),
                    }
    except OSError:
        pass
    return records


def save_feed(
    records: dict[tuple[str, str], dict],
    path: Path = FEED_FILE,
    max_bytes: int = MAX_FEED_BYTES,
    now_ts: float | None = None,
    cfg: IndexerConfig | None = None,
    dry: bool = False,
) -> tuple[int, int]:
    ts = int(now_ts or time.time())
    before = len(records)
    scored: list[tuple[float, dict]] = []

    for row in records.values():
        if _should_evict(row, ts, cfg=cfg):
            continue
        scored.append((_calc_effective_score(row, ts), row))

    scored.sort(key=lambda item: (-item[0], -int(item[1].get("ts", 0))))

    seen_sigs: set[tuple[str, str]] = set()
    out_lines: list[str] = []
    total_bytes = 0

    for _, row in scored:
        sig = (row["cat"], _token_signature(row["q"]))
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)

        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        b_len = len(line.encode("utf-8"))
        if total_bytes + b_len > max_bytes:
            break
        out_lines.append(line)
        total_bytes += b_len

    kept = len(out_lines)
    purged = max(0, before - kept)

    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("".join(out_lines), encoding="utf-8")
        os.replace(tmp, path)

    return kept, purged


def push_candidates(
    cfg: IndexerConfig,
    candidates: list[tuple[str, str, str]],
    path: Path = FEED_FILE,
    dry: bool = False,
) -> int:
    ts = int(time.time())
    records = load_feed(path)
    accepted = 0

    for raw_q, raw_cat, src in candidates:
        q = _norm_q(raw_q)
        cat = (raw_cat or "").strip().lower()
        pts = score_query(cfg, q, cat, src)
        if pts <= 0:
            continue

        key = (cat, q)
        if key in records:
            old = records[key]
            old["s"] = min(20, max(int(old.get("s", 0)), pts) + 1)
            old["ts"] = ts
            old["src"] = src
            if int(old.get("misses", 0)) > 0:
                old["misses"] = int(old["misses"]) - 1
        else:
            records[key] = {
                "q": q,
                "cat": cat,
                "s": pts,
                "src": src,
                "ts": ts,
                "used": 0,
                "hits": 0,
                "misses": 0,
            }
            accepted += 1

    save_feed(records, path=path, now_ts=ts, cfg=cfg, dry=dry)
    return accepted


def pop_queries_for_indexing(
    cat: str,
    limit: int = 12,
    path: Path = FEED_FILE,
    cfg: IndexerConfig | None = None,
    mark_used: bool = True,
    dry: bool = False,
) -> list[str]:
    """Ambil N query dengan komposisi 65% terbukti gacor + 35% kandidat segar."""
    ts = int(time.time())
    records = load_feed(path)
    c = (cat or "").strip().lower()
    cooldown_sec = MIN_REUSE_HOURS * 3600

    proven: list[tuple[float, str]] = []
    fresh: list[tuple[float, str]] = []

    for (rcat, q), row in records.items():
        if rcat != c or _should_evict(row, ts, cfg=cfg):
            continue
        if (ts - int(row.get("used", 0))) < cooldown_sec:
            continue
        eff = _calc_effective_score(row, ts)
        if int(row.get("hits", 0)) > 0:
            proven.append((eff, q))
        else:
            fresh.append((eff, q))

    proven.sort(key=lambda x: -x[0])
    fresh.sort(key=lambda x: -x[0])

    n_fresh = max(1, int(limit * 0.35)) if fresh else 0
    n_proven = limit - n_fresh

    picked = [q for _, q in proven[:n_proven]] + [q for _, q in fresh[:n_fresh]]
    if len(picked) < limit:
        rest = [q for _, q in (proven[n_proven:] + fresh[n_fresh:])]
        picked.extend(rest[: limit - len(picked)])

    random.shuffle(picked)
    selected = picked[:limit]

    if mark_used and selected and not dry:
        for q in selected:
            if (c, q) in records:
                records[(c, q)]["used"] = ts
        save_feed(records, path=path, now_ts=ts, cfg=cfg, dry=False)

    return selected


def report_query_outcome(
    cat: str,
    q: str,
    domains_found: int,
    path: Path = FEED_FILE,
    cfg: IndexerConfig | None = None,
    dry: bool = False,
) -> str:
    """Umpan balik nyata dari Gatekeeper ke pool query."""
    ts = int(time.time())
    records = load_feed(path)
    key = ((cat or "").strip().lower(), _norm_q(q))
    if key not in records:
        return "missing"

    row = records[key]
    row["used"] = ts

    if domains_found > 0:
        row["hits"] = int(row.get("hits", 0)) + min(5, domains_found)
        row["misses"] = 0
        row["s"] = min(25, int(row.get("s", 1)) + 1)
        row["ts"] = ts
        status = "boosted"
    else:
        row["misses"] = int(row.get("misses", 0)) + 1
        row["s"] = int(row.get("s", 1)) - 2
        if _should_evict(row, ts, cfg=cfg):
            del records[key]
            status = "deleted"
        else:
            status = "penalized"

    save_feed(records, path=path, now_ts=ts, cfg=cfg, dry=dry)
    return status
