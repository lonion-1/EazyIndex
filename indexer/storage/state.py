# Copyright (c) 2026 EazyWhale
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manajemen state JSONL append-only, cache veto entitas legal, & ekspor kandidat kaya ke Radar (Tier 2)."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

from ..config_loader import ROOT, IndexerConfig
from ..models import GateVerdict
from ..text_utils import is_subdomain_of

CACHE_DIR = ROOT / ".cache" / "webindex"
CAND_DIR = ROOT / "Candidates"


def _dumps(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(_dumps(r) + "\n")
    os.replace(tmp, path)


def append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(_dumps(r) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    """Baca JSONL baris-per-baris; baris korup/bukan-dict di-skip, bukan membuang seluruh file."""
    if not path.exists():
        return []
    out: list[dict] = []
    corrupt = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except ValueError:
                    corrupt += 1
                    continue
                if not isinstance(obj, dict):
                    corrupt += 1
                    continue
                out.append(obj)
    except OSError:
        pass
    if corrupt:
        print(f"[STATE] {path.name}: skip {corrupt} baris korup", flush=True)
    return out


def read_jsonl_gz(path: Path) -> list[dict]:
    """Baca gzip JSONL; baris korup + EOFError di tail di-skip tanpa membuang baris valid sebelumnya."""
    if not path.exists():
        return []
    out: list[dict] = []
    corrupt = 0
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except ValueError:
                    corrupt += 1
                    continue
                if not isinstance(obj, dict):
                    corrupt += 1
                    continue
                out.append(obj)
    except EOFError:
        # tail .gz rusak — baris valid sebelum EOF tetap dipakai
        pass
    except OSError:
        pass
    if corrupt:
        print(f"[STATE] {path.name}: skip {corrupt} baris korup", flush=True)
    return out


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def flush_trash_dedup(trash_path: Path, gz_path: Path, max_lines: int = 15000) -> int:
    """Kompaksi trash_seen.jsonl dengan deduplikasi sebelum dipindahkan ke arsip .gz."""
    if _line_count(trash_path) < max_lines:
        return 0
    latest_by_dom: dict[str, dict] = {}
    for row in read_jsonl_gz(gz_path) + read_jsonl(trash_path):
        d = row.get("d")
        if d:
            latest_by_dom[d] = row
    gz_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_gz = gz_path.with_name(gz_path.name + ".tmp")
    with gzip.open(tmp_gz, "wt", encoding="utf-8") as f_out:
        for row in latest_by_dom.values():
            f_out.write(_dumps(row) + "\n")
    os.replace(tmp_gz, gz_path)
    trash_path.write_text("", encoding="utf-8")
    return len(latest_by_dom)


class IndexerState:
    def __init__(self, cfg: IndexerConfig):
        self.cfg = cfg
        self.frontier: dict[str, dict] = {}
        self.seen: dict[str, dict] = {}
        self.vetoed: dict[str, dict] = {}
        self.candidates: dict[str, dict] = {}
        self.review: dict[str, dict] = {}
        self.qdone: dict[str, int] = {}

        self._dirty_seen: set[str] = set()
        self._dirty_vetoed: set[str] = set()
        self._dirty_qdone: set[str] = set()

    def load(self) -> None:
        for r in read_jsonl(CACHE_DIR / "frontier.jsonl"):
            u = r.pop("u", None)
            if u:
                self.frontier[u] = r

        all_seen_rows = (
            read_jsonl_gz(CACHE_DIR / "trash_archive.jsonl.gz")
            + read_jsonl(CACHE_DIR / "trash_seen.jsonl")
            + read_jsonl(CACHE_DIR / "seen.jsonl")
        )
        all_seen_rows.sort(key=lambda x: int(x.get("t", 0)))
        for r in all_seen_rows:
            d = r.pop("d", None)
            if d:
                self.seen[d] = r

        for r in read_jsonl(CACHE_DIR / "vetoed_benign.jsonl"):
            d = r.pop("d", None)
            if d:
                self.vetoed[d] = r

        for r in read_jsonl(CACHE_DIR / "queries.jsonl"):
            q = r.get("q")
            if q:
                self.qdone[q] = int(r.get("t", 0))

        for r in read_jsonl(CAND_DIR / "index.jsonl"):
            d = r.pop("d", None)
            if d:
                self.candidates[d] = r
        for r in read_jsonl(CAND_DIR / "review.jsonl"):
            d = r.pop("d", None)
            if d:
                self.review[d] = r

    def record_verdict(self, verdict: GateVerdict, via: str, ts: int) -> None:
        dom = verdict.domain
        self._dirty_seen.add(dom)

        if verdict.decision == "VETO_LEGAL":
            self.seen[dom] = {"k": "veto", "t": ts, "l": verdict.legal_score}
            self.vetoed[dom] = {
                "t": ts,
                "l": verdict.legal_score,
                "reasons": verdict.legal_reasons[:5],
            }
            self._dirty_vetoed.add(dom)
            self.candidates.pop(dom, None)
            self.review.pop(dom, None)
            return

        if verdict.decision == "ACCEPT":
            self.seen[dom] = {
                "k": "ok",
                "t": ts,
                "cat": verdict.category,
                "s": verdict.illegal_score,
                "l": verdict.legal_score,
            }
            old = self.candidates.get(dom)
            vias = (old.get("via", []) if old else []) + [via]
            self.candidates[dom] = {
                "cat": verdict.category,
                "s": verdict.illegal_score,
                "legal_s": verdict.legal_score,
                "pillars": verdict.pillars,
                "reasons": verdict.reasons[:8],
                "landing": verdict.landing_url,
                "probed": verdict.probed_subpage,
                "outbound": verdict.outbound_domains[:15],
                "title": verdict.title[:120],
                "via": list(dict.fromkeys(vias))[-5:],
                "n": (int(old.get("n", 0)) + 1) if old else 1,
                "last": ts,
            }
            self.review.pop(dom, None)
            return

        if verdict.decision == "REVIEW":
            self.seen[dom] = {
                "k": "review",
                "t": ts,
                "cat": verdict.category,
                "s": verdict.illegal_score,
                "l": verdict.legal_score,
            }
            if dom not in self.candidates:
                self.review[dom] = {
                    "cat": verdict.category,
                    "s": verdict.illegal_score,
                    "legal_s": verdict.legal_score,
                    "pillars": verdict.pillars,
                    "reasons": verdict.reasons[:6],
                    "landing": verdict.landing_url,
                    "title": verdict.title[:120],
                    "via": [via],
                    "last": ts,
                }
            return

        self.seen[dom] = {
            "k": "reject",
            "t": ts,
            "s": verdict.illegal_score,
            "l": verdict.legal_score,
        }
        self.candidates.pop(dom, None)
        self.review.pop(dom, None)

    def record_fetch_failure(self, dom: str, kind: str, ts: int) -> None:
        self._dirty_seen.add(dom)
        mapped = "dead" if kind == "gone" else kind
        self.seen[dom] = {"k": mapped, "t": ts}

    def save(self, dry: bool = False) -> dict:
        R = self.cfg.run
        fm = int(R.get("frontier_max", 30000))
        if len(self.frontier) > fm:
            keep = sorted(self.frontier.items(), key=lambda kv: -kv[1].get("p", 0))[:fm]
            self.frontier = dict(keep)

        if dry:
            return {
                "candidates": len(self.candidates),
                "review": len(self.review),
                "vetoed": len(self.vetoed),
            }

        write_jsonl_atomic(
            CACHE_DIR / "frontier.jsonl",
            [{"u": u, **m} for u, m in sorted(self.frontier.items())],
        )

        ok_rows: list[dict] = []
        trash_rows: list[dict] = []
        for d in sorted(self._dirty_seen):
            m = self.seen.get(d)
            if not m:
                continue
            if m.get("k") in ("ok", "review"):
                ok_rows.append({"d": d, **m})
            else:
                trash_rows.append(
                    {"d": d, "k": m.get("k", "reject"), "t": m.get("t", 0)}
                )

        seen_path = CACHE_DIR / "seen.jsonl"
        if _line_count(seen_path) > 35_000:
            write_jsonl_atomic(
                seen_path,
                [
                    {"d": d, **m}
                    for d, m in sorted(self.seen.items())
                    if m.get("k") in ("ok", "review")
                ],
            )
        else:
            append_jsonl(seen_path, ok_rows)

        append_jsonl(CACHE_DIR / "trash_seen.jsonl", trash_rows)
        flush_trash_dedup(
            CACHE_DIR / "trash_seen.jsonl",
            CACHE_DIR / "trash_archive.jsonl.gz",
            max_lines=int(R.get("trash_flush_lines", 15000)),
        )

        if self._dirty_vetoed:
            write_jsonl_atomic(
                CACHE_DIR / "vetoed_benign.jsonl",
                [{"d": d, **m} for d, m in sorted(self.vetoed.items())],
            )

        q_path = CACHE_DIR / "queries.jsonl"
        if _line_count(q_path) > len(self.qdone) + 5_000:
            write_jsonl_atomic(
                q_path, [{"q": q, "t": t} for q, t in sorted(self.qdone.items())]
            )
        elif self._dirty_qdone:
            append_jsonl(
                q_path,
                [
                    {"q": q, "t": self.qdone[q]}
                    for q in sorted(self._dirty_qdone)
                    if q in self.qdone
                ],
            )

        CAND_DIR.mkdir(parents=True, exist_ok=True)
        export_max = int(R.get("export_max", 40000))

        clean_cands = {
            d: r
            for d, r in self.candidates.items()
            if not is_subdomain_of(d, self.cfg.allowlist) and d not in self.vetoed
        }
        sorted_cands = dict(
            sorted(
                clean_cands.items(),
                key=lambda kv: (-kv[1].get("s", 0), -kv[1].get("last", 0)),
            )[:export_max]
        )

        by_cat: dict[str, list[tuple[str, dict]]] = {}
        for d, r in sorted_cands.items():
            by_cat.setdefault(r["cat"], []).append((d, r))

        for cat, items in by_cat.items():
            write_jsonl_atomic(
                CAND_DIR / f"{cat}.jsonl",
                [
                    {
                        "d": d,
                        "s": r["s"],
                        "legal_s": r.get("legal_s", 0),
                        "pillars": r.get("pillars", []),
                        "landing": r.get("landing", ""),
                        "outbound": r.get("outbound", []),
                        "title": r.get("title", ""),
                        "last": r.get("last", 0),
                    }
                    for d, r in items
                ],
            )

        for p in CAND_DIR.glob("*.jsonl"):
            if p.stem not in ("index", "review") and p.stem not in by_cat:
                p.unlink()

        write_jsonl_atomic(
            CAND_DIR / "index.jsonl",
            [{"d": d, **sorted_cands[d]} for d in sorted(sorted_cands)],
        )

        clean_rev = {
            d: r
            for d, r in self.review.items()
            if d not in sorted_cands
            and d not in self.vetoed
            and not is_subdomain_of(d, self.cfg.allowlist)
        }
        sorted_rev = dict(
            sorted(
                clean_rev.items(),
                key=lambda kv: (-kv[1].get("s", 0), -kv[1].get("last", 0)),
            )[:10000]
        )
        write_jsonl_atomic(
            CAND_DIR / "review.jsonl",
            [{"d": d, **sorted_rev[d]} for d in sorted(sorted_rev)],
        )

        return {
            "candidates": len(sorted_cands),
            "review": len(sorted_rev),
            "vetoed": len(self.vetoed),
        }
