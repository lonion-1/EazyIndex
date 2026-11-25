# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Front-Gate Web Indexer (Tier 1) — Orkestrator Utama."""

from __future__ import annotations

import argparse
import heapq
import itertools
import os
import random
import time
import traceback
import urllib.parse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .config_loader import ConfigError, IndexerConfig, load_indexer_config
from .discovery.auto_seed import harvest_autocomplete
from .discovery.engines import EnginePool, SearchHit, build_engine_pool
from .discovery.feed_cache import report_query_outcome
from .discovery.queries import build_mirror_queries, pick_run_queries
from .inspector.dom_parser import parse_html_zones
from .inspector.fetcher import SimpleHttpClient
from .inspector.gatekeeper import decide_gate_verdict, needs_adaptive_probe
from .models import GateVerdict
from .self_test import run_self_test
from .storage.state import IndexerState
from .text_utils import extract_safe_domain

DAY = 86400


def log(tag: str, msg: str) -> None:
    ts = time.strftime("%H:%M:%S", time.gmtime())
    print(f"[{ts}] [{tag}] {msg}", flush=True)


class FrontGateIndexer:
    def __init__(
        self,
        cfg: IndexerConfig,
        state: IndexerState,
        pool: EnginePool,
        http: SimpleHttpClient,
    ):
        self.cfg = cfg
        self.state = state
        self.pool = pool
        self.http = http
        self.R = cfg.run
        self.ts = int(time.time())

        self.heap: list[tuple[float, int, str]] = []
        self.seq = itertools.count()
        for url, meta in self.state.frontier.items():
            heapq.heappush(self.heap, (-float(meta.get("p", 50)), next(self.seq), url))

        self.last_hit: dict[str, float] = {}
        self.new_accepted: list[tuple[str, str]] = []
        self.stats: dict[str, int] = {
            "queries": 0,
            "search_hits": 0,
            "fetched": 0,
            "probed_subpages": 0,
            "accepted": 0,
            "review": 0,
            "vetoed_legal": 0,
            "rejected": 0,
            "failed": 0,
        }

    def push_lead(
        self,
        url: str,
        priority: float,
        via: str,
        hint_cat: str = "",
        force: bool = False,
    ) -> bool:
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False

        dom = extract_safe_domain(
            p.hostname,
            self.cfg.multi_tlds,
            self.cfg.allowlist,
            self.cfg.legal.institutional_tlds,
        )
        if not dom:
            return False

        veto_rec = self.state.vetoed.get(dom)
        if veto_rec and not force:
            veto_days = int(self.R.get("veto_revisit_days", 90))
            if self.ts - int(veto_rec.get("t", 0)) < veto_days * DAY:
                return False

        if not force:
            sd = self.state.seen.get(dom)
            if sd:
                bad = sd.get("k") in ("dead", "blocked", "transient", "ssrf")
                lim = (
                    int(self.R.get("dead_revisit_days", 14))
                    if bad
                    else int(self.R.get("revisit_days", 30))
                )
                if self.ts - int(sd.get("t", 0)) < lim * DAY:
                    return False

        if len(self.state.frontier) >= int(self.R.get("frontier_max", 30000)) * 2:
            return False

        root_url = f"{p.scheme}://{p.netloc.lower()}/"
        cur = self.state.frontier.get(root_url)
        if cur is not None:
            if priority <= float(cur.get("p", 0)):
                return False
            cur["p"] = priority
            if hint_cat and not cur.get("c"):
                cur["c"] = hint_cat
        else:
            self.state.frontier[root_url] = {
                "p": priority,
                "v": via,
                "d": dom,
                "c": hint_cat,
            }

        heapq.heappush(self.heap, (-priority, next(self.seq), root_url))
        return True

    def pop_task(self, inflight_hosts: set[str]) -> tuple[str, dict] | None:
        delay = float(self.R.get("per_host_delay", 1.2))
        deferred: list[tuple[float, int, str]] = []
        found: tuple[str, dict] | None = None
        scanned = 0

        while self.heap and scanned < 300:
            negp, seq, url = heapq.heappop(self.heap)
            scanned += 1
            meta = self.state.frontier.get(url)
            if meta is None or float(meta.get("p", 0)) != -negp:
                continue
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            if self.http.is_tripped(host):
                del self.state.frontier[url]
                continue
            if (
                host in inflight_hosts
                or (time.monotonic() - self.last_hit.get(host, 0.0)) < delay
            ):
                deferred.append((negp, seq, url))
                continue
            found = (url, meta)
            break

        for item in deferred:
            heapq.heappush(self.heap, item)
        if found:
            del self.state.frontier[found[0]]
        return found

    def ingest_search_hit(self, hit: SearchHit, cat: str, q: str) -> bool:
        dom = extract_safe_domain(
            hit.url,
            self.cfg.multi_tlds,
            self.cfg.allowlist,
            self.cfg.legal.institutional_tlds,
        )
        if not dom:
            return False

        bonus = 0
        if self.cfg.re_any_illegal_domain and self.cfg.re_any_illegal_domain.search(
            dom
        ):
            bonus += 12
        rule = self.cfg.categories.get(cat)
        if (
            rule
            and rule.re_active_intent
            and rule.re_active_intent.search(f"{hit.title} {hit.snippet}".lower())
        ):
            bonus += 8

        return self.push_lead(hit.url, 100.0 + bonus, via=f"q:{q[:45]}", hint_cat=cat)

    def run_search_phase(
        self, queries: list[tuple[str, str]], deadline_ts: float, dry: bool = False
    ) -> None:
        lang = str(self.cfg.engines.get("language", "id"))
        pages = int(self.R.get("search_pages", 2))

        for cat, q in queries:
            if time.monotonic() >= deadline_ts - 8.0:
                break
            raw_hits = 0
            new_leads = 0
            for page in range(1, pages + 1):
                hits = self.pool.search(self.http, q, page, deadline_ts, lang)
                if not hits:
                    break
                raw_hits += len(hits)
                for h in hits:
                    if self.ingest_search_hit(h, cat, q):
                        new_leads += 1
                time.sleep(0.5)

            self.state.qdone[q] = int(time.time())
            self.state._dirty_qdone.add(q)
            self.stats["queries"] += 1
            self.stats["search_hits"] += raw_hits
            report_query_outcome(cat, q, new_leads, cfg=self.cfg, dry=dry)
            log(
                "SEARCH",
                f"[{cat}] {q[:55]!r}: {raw_hits} hasil -> +{new_leads} lead baru",
            )
            time.sleep(0.6)

    def inspect_lead(self, task: dict, deadline_ts: float) -> dict:
        timeout = float(self.R.get("fetch_timeout", 10))
        max_bytes = int(self.R.get("body_max_bytes", 350_000))

        r = self.http.get(
            task["url"], timeout=timeout, max_bytes=max_bytes, deadline_ts=deadline_ts
        )
        if not r.ok:
            return {"task": task, "ok": False, "kind": r.kind}

        final_dom = (
            extract_safe_domain(
                r.url,
                self.cfg.multi_tlds,
                self.cfg.allowlist,
                self.cfg.legal.institutional_tlds,
            )
            or task["dom"]
        )
        low_html = r.text.lower()
        if any(ph in low_html for ph in self.cfg.parked_hints):
            return {"task": task, "ok": False, "kind": "dead"}

        landing_zones = parse_html_zones(r.text, r.url)

        probe_url = needs_adaptive_probe(
            self.cfg, final_dom, landing_zones, hint_cat=task.get("hint_cat", "")
        )
        secondary_zones = None
        probed_used = ""
        if probe_url and time.monotonic() < deadline_ts - 3.0:
            r2 = self.http.get(
                probe_url, timeout=timeout, max_bytes=max_bytes, deadline_ts=deadline_ts
            )
            if r2.ok and r2.text:
                secondary_zones = parse_html_zones(r2.text, r2.url)
                probed_used = r2.url

        verdict = decide_gate_verdict(
            self.cfg,
            final_dom,
            landing_zones,
            hint_cat=task.get("hint_cat", ""),
            secondary_zones=secondary_zones,
            probed_url=probed_used,
        )
        return {"task": task, "ok": True, "verdict": verdict}

    def run_inspection_phase(self, deadline_ts: float) -> None:
        workers = int(self.R.get("workers", 12))
        ex = ThreadPoolExecutor(max_workers=workers)
        inflight: dict = {}
        inflight_hosts: set[str] = set()
        last_log = time.monotonic()

        try:
            while time.monotonic() < deadline_ts - 3.0:
                while len(inflight) < workers * 2:
                    t = self.pop_task(inflight_hosts)
                    if t is None:
                        break
                    url, meta = t
                    host = (urllib.parse.urlparse(url).hostname or "").lower()
                    task = {
                        "url": url,
                        "dom": meta["d"],
                        "via": meta.get("v", ""),
                        "hint_cat": meta.get("c", ""),
                        "host": host,
                    }
                    self.last_hit[host] = time.monotonic()
                    inflight_hosts.add(host)
                    inflight[ex.submit(self.inspect_lead, task, deadline_ts)] = task

                if not inflight:
                    if not self.heap:
                        break
                    time.sleep(0.25)
                    continue

                done, _ = wait(list(inflight), timeout=2.5, return_when=FIRST_COMPLETED)
                for fut in done:
                    task = inflight.pop(fut)
                    inflight_hosts.discard(task["host"])
                    self.stats["fetched"] += 1
                    try:
                        res = fut.result()
                    except Exception as e:
                        self.stats["failed"] += 1
                        if self.stats["failed"] <= 5:
                            log("ERROR", f"{task['url'][:65]}: {e!r}")
                        continue

                    now_ts = int(time.time())
                    if not res.get("ok"):
                        self.stats["failed"] += 1
                        self.state.record_fetch_failure(
                            task["dom"], res.get("kind", "transient"), now_ts
                        )
                        continue

                    verdict: GateVerdict = res["verdict"]
                    if verdict.probed_subpage:
                        self.stats["probed_subpages"] += 1

                    self.state.record_verdict(verdict, task["via"], now_ts)
                    if verdict.domain != task["dom"]:
                        self.state.record_fetch_failure(
                            task["dom"], "redirected", now_ts
                        )

                    if verdict.decision == "ACCEPT":
                        self.stats["accepted"] += 1
                        self.new_accepted.append((verdict.category, verdict.domain))
                    elif verdict.decision == "REVIEW":
                        self.stats["review"] += 1
                    elif verdict.decision == "VETO_LEGAL":
                        self.stats["vetoed_legal"] += 1
                    else:
                        self.stats["rejected"] += 1

                if time.monotonic() - last_log > 25.0:
                    last_log = time.monotonic()
                    log(
                        "GATE",
                        f"checked={self.stats['fetched']} accept={self.stats['accepted']} "
                        f"review={self.stats['review']} veto_legal={self.stats['vetoed_legal']} "
                        f"reject={self.stats['rejected']} frontier={len(self.state.frontier)} "
                        f"sisa={int(deadline_ts - time.monotonic())}s",
                    )
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    def collect_mirror_brands(self, limit: int) -> list[tuple[str, str]]:
        brands: list[tuple[str, str]] = []
        for cat, dom in self.new_accepted:
            lab = dom.split(".")[0]
            if len(lab) >= 4 and (cat, lab) not in brands:
                brands.append((cat, lab))
        for cname, rule in self.cfg.categories.items():
            for b in rule.queries_cfg.get("brands", []):
                clean = str(b).strip().lower()
                if len(clean) >= 4 and (cname, clean) not in brands:
                    brands.append((cname, clean))
        random.shuffle(brands)
        return brands[:limit]


def _write_step_summary(
    st: dict, exp: dict, accepted_samples: list[tuple[str, str]], dry: bool
) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        f"## 🛡️ Front-Gate Web Indexer{' (dry-run)' if dry else ''}",
        "",
        "| Metrik | Nilai |",
        "|---|---|",
    ]
    for k, v in sorted(st.items()):
        lines.append(f"| {k} | {v} |")
    lines.append(f"| total_candidates_active | {exp.get('candidates', 0)} |")
    lines.append(f"| total_review_queue | {exp.get('review', 0)} |")
    lines.append(f"| total_vetoed_benign | {exp.get('vetoed', 0)} |")
    lines.append("")
    if accepted_samples:
        preview = ", ".join(f"`{d}` ({c})" for c, d in accepted_samples[:30])
        lines.append(
            f"**Kandidat lolos gerbang ({len(accepted_samples)} baru):** {preview}"
        )
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Front-Gate Web Indexer (Tier 1)")
    ap.add_argument(
        "--check-config",
        action="store_true",
        help="Validasi TOML & jalankan anti-FP self-test",
    )
    ap.add_argument("--budget", type=int, default=0, help="Anggaran waktu dalam detik")
    ap.add_argument("--workers", type=int, default=0, help="Jumlah worker paralel")
    ap.add_argument(
        "--cats", default="", help="Filter kategori (koma, mis. comic,film,judol)"
    )
    ap.add_argument(
        "--engines", default="", help="Filter engine (koma, mis. searxng,ddg_html)"
    )
    ap.add_argument("--queries", type=int, default=0, help="Jumlah query per sesi")
    ap.add_argument(
        "--skip-suggest", action="store_true", help="Lewati panen autocomplete awal"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Jalankan penuh tanpa menulis file ke disk",
    )
    args = ap.parse_args(argv)

    try:
        cfg = load_indexer_config()
    except ConfigError as e:
        log("CONFIG", f"GAGAL: {e}")
        return 2

    if args.check_config:
        ok = run_self_test(cfg)
        return 0 if ok else 1

    if args.workers > 0:
        cfg.run["workers"] = args.workers

    cats = {c.strip().lower() for c in args.cats.split(",") if c.strip()} or None
    only_eng = {e.strip().lower() for e in args.engines.split(",") if e.strip()} or None
    budget = args.budget or int(cfg.run.get("time_budget_seconds", 3300))
    repeat_days = int(cfg.run.get("query_repeat_days", 7))

    if not args.skip_suggest:
        try:
            s_res = harvest_autocomplete(cfg, max_req=28, cats=cats, dry=args.dry_run)
            log(
                "SUGGEST",
                f"Panen selesai: {s_res['requests']} req -> +{s_res['accepted']} query baru (pool={s_res['pool']})",
            )
        except Exception as e:
            log("SUGGEST", f"Dilewati karena error ringan: {e!r}")

    state = IndexerState(cfg)
    state.load()
    log(
        "STATE",
        f"Loaded: frontier={len(state.frontier)}, seen={len(state.seen)}, "
        f"vetoed={len(state.vetoed)}, candidates={len(state.candidates)}",
    )

    pool = build_engine_pool(cfg.engines, cfg.multi_tlds, only=only_eng)
    log(
        "ENGINE",
        "Mesin aktif: " + (", ".join(e.name for e in pool.engines) or "TIDAK ADA"),
    )

    http = SimpleHttpClient(dry=args.dry_run)
    ix = FrontGateIndexer(cfg, state, pool, http)

    t0 = time.monotonic()
    end_ts = t0 + max(30, budget - 45)
    exp = {
        "candidates": len(state.candidates),
        "review": len(state.review),
        "vetoed": len(state.vetoed),
    }

    try:
        q_target = args.queries or int(cfg.run.get("queries_per_run", 65))
        qs = pick_run_queries(
            cfg, state.qdone, ix.ts, q_target, repeat_days, cats=cats, dry=args.dry_run
        )
        log("PLAN", f"{len(qs)} query disiapkan untuk tahap pencarian awal")

        search_deadline = t0 + (end_ts - t0) * 0.28
        ix.run_search_phase(qs, search_deadline, dry=args.dry_run)

        inspect1_deadline = t0 + (end_ts - t0) * 0.78
        ix.run_inspection_phase(inspect1_deadline)

        n_mirror = int(cfg.run.get("mirror_queries_per_run", 35))
        brands = ix.collect_mirror_brands(max(6, n_mirror // 2))
        mqs = build_mirror_queries(
            cfg, brands, n_mirror, state.qdone, ix.ts, repeat_days
        )
        if mqs and time.monotonic() < end_ts - 15.0:
            log("MIRROR", f"{len(mqs)} query perburuan mirror dijalankan")
            ix.run_search_phase(mqs, t0 + (end_ts - t0) * 0.88, dry=args.dry_run)
            ix.run_inspection_phase(end_ts)
    except Exception as e:
        log("ERROR", f"Indexer crash: {e!r}")
        traceback.print_exc()
    finally:
        try:
            exp = state.save(dry=args.dry_run)
        except Exception as e:
            log("ERROR", f"Gagal menyimpan state: {e!r}")

    elapsed = round(time.monotonic() - t0)
    st = ix.stats
    log(
        "HASIL",
        f"{elapsed}s | query={st['queries']} hits={st['search_hits']} checked={st['fetched']} "
        f"(subprobe={st['probed_subpages']}) | ACCEPT={st['accepted']} REVIEW={st['review']} "
        f"VETO_LEGAL={st['vetoed_legal']} REJECT={st['rejected']} | total_candidates={exp['candidates']}",
    )
    _write_step_summary(st, exp, ix.new_accepted, args.dry_run)
    return 0
