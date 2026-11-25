# Copyright (c) 2026 EazyWhale
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generator query terpadu: kombinasi TOML statis + feed_cache adaptif + perburuan mirror."""
from __future__ import annotations

import random
import time

from ..config_loader import IndexerConfig
from ..text_utils import norm_space
from .feed_cache import pop_queries_for_indexing

DAY = 86400


def build_static_queries(cfg: IndexerConfig, ts: float, cats: set[str] | None = None) -> list[tuple[str, str]]:
    year = str(time.gmtime(ts).tm_year)
    out: list[tuple[str, str]] = []
    for cat, rule in cfg.categories.items():
        if cats and cat not in cats:
            continue
        qc = rule.queries_cfg
        for q in qc.get("plain", []):
            nq = norm_space(str(q).replace("{year}", year)).lower()
            if nq:
                out.append((cat, nq))
        fillers = [str(f).strip() for f in qc.get("fillers", []) if str(f).strip()]
        for tpl in qc.get("templates", []):
            t_str = str(tpl)
            if "{t}" in t_str:
                for f in fillers:
                    nq = norm_space(t_str.replace("{t}", f).replace("{year}", year)).lower()
                    if nq:
                        out.append((cat, nq))
            else:
                nq = norm_space(t_str.replace("{year}", year)).lower()
                if nq:
                    out.append((cat, nq))
    return list(dict.fromkeys(out))


def pick_run_queries(
    cfg: IndexerConfig,
    done: dict[str, int],
    ts: float,
    total_n: int,
    repeat_days: int,
    cats: set[str] | None = None,
    dry: bool = False,
) -> list[tuple[str, str]]:
    """Gabungkan query dari self-pruning feed_cache dan template statis secara seimbang per kategori."""
    active_cats = [c for c in cfg.categories if not cats or c in cats]
    if not active_cats:
        return []

    per_cat_feed = max(2, (total_n // 2) // len(active_cats))
    feed_by_cat: dict[str, list[tuple[str, str]]] = {}
    for cat in active_cats:
        popped = pop_queries_for_indexing(cat, limit=per_cat_feed, cfg=cfg, mark_used=True, dry=dry)
        feed_by_cat[cat] = [(cat, q) for q in popped]

    static_all = build_static_queries(cfg, ts, cats)
    fresh_by_cat: dict[str, list[tuple[str, str]]] = {}
    stale_pool: list[tuple[float, str, str]] = []

    for cat, q in static_all:
        last_t = float(done.get(q, 0))
        if last_t == 0 or (ts - last_t) > repeat_days * DAY:
            fresh_by_cat.setdefault(cat, []).append((cat, q))
        else:
            stale_pool.append((last_t, cat, q))

    for lst in fresh_by_cat.values():
        never = [it for it in lst if done.get(it[1], 0) == 0]
        rerun = [it for it in lst if done.get(it[1], 0) > 0]
        random.shuffle(never)
        random.shuffle(rerun)
        lst[:] = rerun + never

    out: list[tuple[str, str]] = []
    seen_q: set[str] = set()

    def _add(item: tuple[str, str]) -> bool:
        if item[1] in seen_q:
            return False
        seen_q.add(item[1])
        out.append(item)
        return True

    while len(out) < total_n and (any(feed_by_cat.values()) or any(fresh_by_cat.values())):
        for cat in active_cats:
            if len(out) >= total_n:
                break
            if feed_by_cat.get(cat):
                _add(feed_by_cat[cat].pop())
            if len(out) < total_n and fresh_by_cat.get(cat):
                _add(fresh_by_cat[cat].pop())

    if len(out) < total_n and stale_pool:
        stale_pool.sort(key=lambda x: x[0])
        for _, cat, q in stale_pool:
            if len(out) >= total_n:
                break
            _add((cat, q))

    return out


def build_mirror_queries(
    cfg: IndexerConfig,
    brands_with_cat: list[tuple[str, str]],
    n: int,
    done: dict[str, int],
    ts: float,
    repeat_days: int,
) -> list[tuple[str, str]]:
    """Hasilkan query perburuan mirror dari merek yang baru ditemukan & merek bawaan kategori."""
    tpls = cfg.mirror_templates
    if not tpls:
        return []
    raw: list[tuple[str, str]] = []
    for cat, b in brands_with_cat:
        clean_b = norm_space(b).lower()
        if len(clean_b) < 4:
            continue
        for tpl in tpls:
            nq = norm_space(tpl.replace("{b}", clean_b)).lower()
            if nq:
                raw.append((cat, nq))

    dedup: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if item[1] not in seen:
            seen.add(item[1])
            dedup.append(item)

    fresh = [it for it in dedup if it[1] not in done or (ts - float(done.get(it[1], 0))) > repeat_days * DAY]
    stale = [it for it in dedup if it not in fresh]
    random.shuffle(fresh)
    stale.sort(key=lambda it: float(done.get(it[1], 0)))
    return (fresh + stale)[:n]
