# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pemanen saran kata kunci (multi-provider autocomplete) -> feed_cache.py."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .indexer.config_loader import IndexerConfig
from . import reporting
from .config import HarvestConfig, load_auto_suggest
from ..discovery.feed_cache import FEED_FILE, load_feed, push_candidates
from .filters import FilterEngine
from .http_client import HttpClient
from .locale_detect import LocaleDetector
from .providers import ProviderBlocked, ProviderError, load_providers
from .query_builder import build_probe_query, clean_suggestion
from .router import choose_source


@dataclass
class HarvestStats:
    requests: int = 0
    hits_by_src: dict[str, int] = field(default_factory=dict)
    err_by_src: dict[str, int] = field(default_factory=dict)
    candidates: list[tuple[str, str, str]] = field(default_factory=list)

    def record_hit(self, src: str) -> None:
        self.hits_by_src[src] = self.hits_by_src.get(src, 0) + 1
        self.err_by_src[src] = 0


def _seed_pool(
    cfg: IndexerConfig, cat: str, existing_feed: dict, chain_k: int
) -> list[str]:
    q_cfg = cfg.categories[cat].queries_cfg
    base = list(
        dict.fromkeys(
            s.strip()
            for s in (list(q_cfg.get("fillers", [])) + list(q_cfg.get("brands", [])))
            if s and s.strip()
        )
    )
    chained = [
        q
        for (rcat, q), row in existing_feed.items()
        if rcat == cat and row.get("hits", 0) > 0
    ]
    sampled = random.sample(chained, k=min(chain_k, len(chained))) if chained else []
    return list(dict.fromkeys(base + sampled))


def harvest_autocomplete(
    cfg: IndexerConfig,
    max_req: int = 30,
    cats: set[str] | None = None,
    dry: bool = False,
    *,
    hcfg: HarvestConfig | None = None,
) -> dict:
    """Panen saran autocomplete dan setorkan langsung ke feed_cache.py."""
    hcfg = hcfg or load_auto_suggest()
    h = hcfg.harvest
    year = hcfg.resolve_year()

    detector = LocaleDetector.from_config(hcfg.locale)
    providers = load_providers(hcfg)
    client = HttpClient(hcfg.http)
    filter_engine = FilterEngine.from_config(hcfg.filter)
    brand_tokens_by_cat = {
        c: list(cfg.categories[c].queries_cfg.get("brands", [])) for c in cfg.categories
    }

    stats = HarvestStats()
    disabled: set[str] = set()

    existing_feed = load_feed(FEED_FILE)
    cat_names = [c for c in cfg.categories if not cats or c in cats]
    random.shuffle(cat_names)
    per_cat_quota = max(h.get("per_cat_min", 2), max_req // max(1, len(cat_names)))

    min_words = h.get("min_words", 1)
    max_words = h.get("max_words", 5)
    min_len = h.get("min_len", 4)
    max_len = h.get("max_len", 48)
    require_portal = h.get("require_portal_or_brand", True)
    reject_prefixes = tuple(h.get("reject_prefixes", ("http://", "https://")))

    for cat in cat_names:
        if stats.requests >= max_req:
            break

        pool = _seed_pool(cfg, cat, existing_feed, h.get("chain_sample_k", 2))
        if not pool:
            continue

        for seed in random.sample(pool, k=min(per_cat_quota, len(pool))):
            if stats.requests >= max_req:
                break

            hl, gl = detector.detect(seed)
            source = choose_source(
                gl,
                hcfg.routing_default,
                hcfg.routing_override,
                available=set(providers),
                disabled=disabled,
            )
            if source is None:
                stats.requests = max_req
                break

            provider = providers[source]
            q_query = build_probe_query(
                seed, cat, year, hcfg.probe, hcfg.modifiers, hl=hl
            )
            stats.requests += 1

            try:
                suggestions = client.fetch(provider, q_query, hl, gl)
                for s in suggestions:
                    if not filter_engine.accept(
                        s,
                        cat=cat,
                        hl=hl,
                        brand_tokens=brand_tokens_by_cat.get(cat, ()),
                        require_portal_or_brand=require_portal,
                        min_words=min_words,
                        max_words=max_words,
                        min_len=min_len,
                        max_len=max_len,
                        reject_prefixes=reject_prefixes,
                    ):
                        continue
                    clean = clean_suggestion(
                        s,
                        min_len=min_len,
                        max_len=max_len,
                        reject_prefixes=reject_prefixes,
                    )
                    if clean:
                        stats.candidates.append((clean, cat, f"suggest:{source}"))
                        stats.record_hit(source)
            except ProviderBlocked:
                disabled.add(source)
            except ProviderError:
                n = stats.err_by_src.get(source, 0) + 1
                stats.err_by_src[source] = n
                if n >= hcfg.http.get("disable_after_n_err", 3):
                    disabled.add(source)
            except Exception:
                stats.err_by_src[source] = stats.err_by_src.get(source, 0) + 1

            client.sleep_jitter()

    accepted = push_candidates(cfg, stats.candidates, dry=dry)
    total_pool = len(load_feed(FEED_FILE))
    result = {
        "requests": stats.requests,
        "accepted": accepted,
        "pool": total_pool,
        "sources": stats.hits_by_src,
    }
    reporting.write_summary(result, dry=dry)
    return result
    total_pool = len(load_feed(FEED_FILE))
    result = {
        "requests": stats.requests,
        "accepted": accepted,
        "pool": total_pool,
        "sources": stats.hits_by_src,
    }
    reporting.write_summary(result, dry=dry)
    return result
