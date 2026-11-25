# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Archive Manager (Gudang Beku & Zombie Resurrector):
- Simpan & baca domain dalam format .jsonl maupun Archive/<cat>.jsonl.gz (deterministik, mtime=0).
- Gabungkan riwayat skor/sumber tanpa menimpa metadata lama & bersihkan domain allowlist.
- Bangkitkan kembali (resurrect) domain di Archive/ dengan exponential backoff anti-ping-pong.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import json
import os
import random
import re
import socket
import sys
import time
import zlib
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent if _HERE.name == "scripts" else _HERE
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ARCHIVE_DIR = ROOT / "Archive"
ALLOWLIST_FILE = ROOT / "allowlist.txt"

DAY = 86400
DEFAULT_DORMANT_DAYS = 7
DEFAULT_MAX_RESURRECT = 4

RE_VALID_DOMAIN = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9][a-z0-9-]{1,22}[a-z0-9]$"
)


def now() -> float:
    return time.time()


def log(tag: str, msg: str) -> None:
    ts = time.strftime("%H:%M:%S", time.gmtime())
    print(f"[{ts}] [{tag}] {msg}", flush=True)


class Deadline:
    def __init__(self, timeout_sec: float) -> None:
        self._end = time.monotonic() + max(0.0, float(timeout_sec))

    def expired(self) -> bool:
        return time.monotonic() >= self._end

    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())


def dns_many(
    domains: list[str],
    workers: int = 32,
    per_timeout: float = 5.0,
    deadline: Deadline | None = None,
) -> dict[str, bool]:
    """Resolve DNS paralel mengembalikan {domain: True/False}."""
    results: dict[str, bool] = {}
    if not domains:
        return results

    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(per_timeout)

    def _probe(dom: str) -> tuple[str, bool]:
        if deadline and deadline.expired():
            return dom, False
        try:
            socket.getaddrinfo(dom, None)
            return dom, True
        except OSError:
            return dom, False

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_probe, d) for d in domains]
            for fut in concurrent.futures.as_completed(futures):
                dom, ok = fut.result()
                results[dom] = ok
    finally:
        socket.setdefaulttimeout(old_timeout)

    return results


def _safe_cat(label: str | None) -> str:
    c = re.sub(r"[^a-z0-9_-]", "", (label or "judol").lower())
    return c or "judol"


def _clean_domain(raw: str | None) -> str:
    if not raw:
        return ""
    s = str(raw).strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.split(r"[\^|$\s#/\\:?]", s.removeprefix("||"), maxsplit=1)[0]
    s = s.lstrip("*.").rstrip(".").removeprefix("www.")
    return s if RE_VALID_DOMAIN.match(s) else ""


def _load_allowlist() -> set[str]:
    out: set[str] = set()
    for path in (ALLOWLIST_FILE, ROOT / "config" / "allowlist.txt"):
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.split("#", 1)[0].strip()
                dom = _clean_domain(s)
                if dom:
                    out.add(dom)
        except OSError:
            pass
    try:
        from indexer.config_loader import load_indexer_config

        cfg = load_indexer_config()
        out.update(cfg.allowlist)
    except Exception:
        pass
    return out


def _normalize_record(raw: dict, cat: str, fallback_ts: int) -> dict | None:
    d = _clean_domain(raw.get("d"))
    if not d:
        return None
    srcs = [str(x).strip() for x in (raw.get("src") or []) if x and str(x).strip()]
    rec: dict = {
        "d": d,
        "cat": _safe_cat(raw.get("cat") or cat),
        "s": max(0, int(raw.get("s") or 0)),
        "status": "archived",
        "src": list(dict.fromkeys(srcs))[-12:] or ["archive"],
        "first": int(raw.get("first") or fallback_ts),
        "last": int(raw.get("last") or fallback_ts),
        "tries": max(1, int(raw.get("tries") or 1)),
        "note": str(raw.get("note") or "archived")[:120],
    }
    if int(raw.get("resurrect_count") or 0) > 0:
        rec["resurrect_count"] = int(raw["resurrect_count"])
    if int(raw.get("archived_at") or 0) > 0:
        rec["archived_at"] = int(raw["archived_at"])
    return rec


def _merge_record(old: dict | None, new: dict, ts: int) -> dict:
    if not old:
        merged = dict(new)
        merged["archived_at"] = ts
        return merged

    merged = dict(old)
    merged["d"] = new["d"]
    merged["cat"] = new["cat"]
    merged["status"] = "archived"
    merged["s"] = max(int(old.get("s", 0)), int(new.get("s", 0)))
    merged["first"] = min(int(old.get("first") or ts), int(new.get("first") or ts))
    merged["last"] = max(int(old.get("last") or ts), int(new.get("last") or ts))
    merged["tries"] = max(int(old.get("tries", 1)), int(new.get("tries", 1)))
    merged["archived_at"] = ts

    old_res = int(old.get("resurrect_count") or 0)
    new_res = int(new.get("resurrect_count") or 0)
    if max(old_res, new_res) > 0:
        merged["resurrect_count"] = max(old_res, new_res)

    combined_src = list(dict.fromkeys((old.get("src") or []) + (new.get("src") or [])))
    merged["src"] = combined_src[-12:] or ["archive"]

    new_note = str(new.get("note") or "").strip()
    if new_note and new_note != "archived":
        merged["note"] = new_note[:120]
    return merged


def read_jsonl(path: Path) -> list[dict]:
    """Baca file .jsonl maupun .jsonl.gz menjadi list of dict."""
    rows: list[dict] = []
    if not path.exists():
        return rows
    opener = (
        (lambda p: gzip.open(p, "rt", encoding="utf-8", errors="ignore"))
        if path.name.endswith(".gz")
        else (lambda p: open(p, "r", encoding="utf-8", errors="ignore"))
    )
    try:
        with opener(path) as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith(("#", "!")):
                    continue
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        rows.append(obj)
                except ValueError:
                    continue
    except (OSError, EOFError, zlib.error) as e:
        log("ARCHIVE", f"peringatan baca {path.name}: {e}")
    return rows


def write_jsonl(path: Path, rows: list[dict], dry: bool = False) -> None:
    """Tulis file .jsonl atau .jsonl.gz secara atomik."""
    if dry:
        return
    if not rows:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if path.name.endswith(".gz"):
        with (
            open(tmp, "wb") as raw_f,
            gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_f, compresslevel=6, mtime=0
            ) as gz,
        ):
            for row in rows:
                gz.write(
                    (
                        json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    ).encode("utf-8")
                )
    else:
        with open(tmp, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(
                    json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
    os.replace(tmp, path)


def read_archive_map(path: Path) -> dict[str, dict]:
    """Baca Archive/<cat>.jsonl.gz, .jsonl, atau format lama *.txt.gz -> {domain: record}."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    cat = _safe_cat(
        path.name.removesuffix(".jsonl.gz")
        .removesuffix(".jsonl")
        .removesuffix(".txt.gz")
    )
    ts = int(now())
    opener = (
        (lambda p: gzip.open(p, "rt", encoding="utf-8", errors="ignore"))
        if path.name.endswith(".gz")
        else (lambda p: open(p, "r", encoding="utf-8", errors="ignore"))
    )
    try:
        with opener(path) as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith(("#", "!")):
                    continue
                if s.startswith("{"):
                    try:
                        row = json.loads(s)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        norm = _normalize_record(row, cat, ts)
                        if norm:
                            out[norm["d"]] = norm
                else:
                    d = _clean_domain(s)
                    if d:
                        out[d] = {
                            "d": d,
                            "cat": cat,
                            "s": 0,
                            "status": "archived",
                            "src": ["legacy_archive"],
                            "first": ts,
                            "last": ts,
                            "tries": 1,
                            "note": "migrated_txt_gz",
                        }
    except (OSError, EOFError, zlib.error) as e:
        log(
            "ARCHIVE",
            f"peringatan baca {path.name} (terselamatkan {len(out)} baris): {e}",
        )
    return out


def write_archive_map(path: Path, records: dict[str, dict], dry: bool = False) -> None:
    """Tulis ulang Archive/<cat>.jsonl.gz (atau .jsonl) secara atomik & deterministik."""
    ordered = sorted(records.values(), key=lambda r: str(r.get("d", "")))
    write_jsonl(path, ordered, dry=dry)


def simpan_arsip_records(cat: str, rows: list[dict], dry: bool = False) -> int:
    """Kompres dan gabungkan record domain ke Archive/<cat>.jsonl.gz."""
    if not rows:
        return 0
    c = _safe_cat(cat)
    gz_path = ARCHIVE_DIR / f"{c}.jsonl.gz"
    allowlist = _load_allowlist()
    existing = read_archive_map(gz_path)
    ts = int(now())
    added = 0
    purged_allow = 0

    for d in [dom for dom in existing if dom in allowlist]:
        del existing[d]
        purged_allow += 1

    for r in rows:
        if not isinstance(r, dict):
            continue
        norm = _normalize_record(r, c, ts)
        if not norm or norm["d"] in allowlist:
            continue
        d = norm["d"]
        if d not in existing:
            added += 1
        existing[d] = _merge_record(existing.get(d), norm, ts)

    if added > 0 or purged_allow > 0 or not gz_path.exists():
        write_archive_map(gz_path, existing, dry=dry)
    log(
        "ARCHIVE",
        f"Tersimpan {len(existing)} domain di {gz_path.name} (+{added} baru"
        + (f", -{purged_allow} allowlist" if purged_allow else "")
        + ")",
    )
    return added


def simpan_arsip(domains: list[str], label: str = "judol", dry: bool = False) -> int:
    """Kompres daftar domain polos ke Archive/<label>.jsonl.gz (gabung jika sudah ada)."""
    if not domains:
        return 0
    ts = int(now())
    c = _safe_cat(label)
    rows = [
        {
            "d": d,
            "cat": c,
            "s": 0,
            "status": "archived",
            "src": ["archive_manual"],
            "first": ts,
            "last": ts,
            "tries": 1,
            "note": "archived",
        }
        for d in domains
        if d and str(d).strip()
    ]
    return simpan_arsip_records(c, rows, dry=dry)


def _is_eligible_for_resurrect(
    rec: dict,
    ts: int,
    min_dormant_days: int,
    max_resurrect: int,
) -> bool:
    res_cnt = int(rec.get("resurrect_count") or 0)
    if res_cnt >= max_resurrect:
        return False
    archived_at = int(rec.get("archived_at") or rec.get("last") or 0)
    if archived_at <= 0:
        return True
    cooldown_sec = min_dormant_days * DAY * (2 ** min(res_cnt, 4))
    return (ts - archived_at) >= cooldown_sec


def bangkitkan_arsip(
    max_cek: int = 500,
    timeout_sec: int = 45,
    dry: bool = False,
    min_dormant_days: int = DEFAULT_DORMANT_DAYS,
    max_resurrect: int = DEFAULT_MAX_RESURRECT,
) -> list[tuple[str, str, dict]]:
    """Cek DNS sampel domain dorman lintas file Archive/ dan bangkitkan yang hidup kembali."""
    if not ARCHIVE_DIR.exists() or max_cek <= 0:
        return []
    files = sorted(
        list(ARCHIVE_DIR.glob("*.jsonl.gz"))
        + list(ARCHIVE_DIR.glob("*.jsonl"))
        + list(ARCHIVE_DIR.glob("*.txt.gz"))
    )
    if not files:
        return []

    ts = int(now())
    allowlist = _load_allowlist()
    loaded: dict[Path, dict[str, dict]] = {}
    eligible_by_file: dict[Path, list[str]] = {}
    dirty_files: set[Path] = set()

    for fpath in files:
        recs = read_archive_map(fpath)
        purged = 0
        for d in [dom for dom in recs if dom in allowlist]:
            del recs[d]
            purged += 1
        if purged > 0 or not fpath.name.endswith(".jsonl.gz"):
            dirty_files.add(fpath)
        loaded[fpath] = recs

        elig = [
            d
            for d, r in recs.items()
            if _is_eligible_for_resurrect(r, ts, min_dormant_days, max_resurrect)
        ]
        if elig:
            eligible_by_file[fpath] = elig

    active_files = list(eligible_by_file.keys())
    random.shuffle(active_files)
    picked_files = active_files[:3]

    sample_map: dict[str, Path] = {}
    if picked_files:
        per_file_quota = max(1, max_cek // len(picked_files))
        for fpath in picked_files:
            pool_doms = eligible_by_file[fpath]
            chosen = random.sample(pool_doms, min(len(pool_doms), per_file_quota))
            for d in chosen:
                sample_map[d] = fpath

        if len(sample_map) < max_cek:
            leftover = [
                (d, fpath)
                for fpath in picked_files
                for d in eligible_by_file[fpath]
                if d not in sample_map
            ]
            if leftover:
                extra = random.sample(
                    leftover, min(len(leftover), max_cek - len(sample_map))
                )
                for d, fpath in extra:
                    sample_map[d] = fpath

    resurrected: list[tuple[str, str, dict]] = []
    if sample_map:
        hasil_dns = dns_many(
            list(sample_map.keys()),
            workers=32,
            per_timeout=5.0,
            deadline=Deadline(timeout_sec),
        )
        for d, status in hasil_dns.items():
            if status is not True:
                continue
            fpath = sample_map.get(d)
            if not fpath or d not in loaded.get(fpath, {}):
                continue
            rec = dict(loaded[fpath].pop(d))
            dirty_files.add(fpath)

            cat = _safe_cat(
                rec.get("cat")
                or fpath.name.removesuffix(".jsonl.gz")
                .removesuffix(".jsonl")
                .removesuffix(".txt.gz")
            )
            res_cnt = int(rec.get("resurrect_count") or 0) + 1
            rec["cat"] = cat
            rec["status"] = "pool"
            rec["last"] = ts
            rec["resurrect_count"] = res_cnt
            rec.pop("archived_at", None)
            rec["note"] = f"resurrected_from_archive(#{res_cnt})"

            srcs = list(rec.get("src") or [])
            if "archive:resurrect" not in srcs:
                srcs.append("archive:resurrect")
            rec["src"] = srcs[-12:]
            resurrected.append((d, cat, rec))

    if not dry:
        for fpath in dirty_files:
            cat = _safe_cat(
                fpath.name.removesuffix(".jsonl.gz")
                .removesuffix(".jsonl")
                .removesuffix(".txt.gz")
            )
            target_jsonl = ARCHIVE_DIR / f"{cat}.jsonl.gz"
            recs = loaded[fpath]
            if target_jsonl != fpath and target_jsonl.exists():
                merged_target = read_archive_map(target_jsonl)
                for d, r in recs.items():
                    merged_target[d] = _merge_record(merged_target.get(d), r, ts)
                write_archive_map(target_jsonl, merged_target, dry=False)
                fpath.unlink(missing_ok=True)
            else:
                write_archive_map(target_jsonl, recs, dry=False)
                if fpath != target_jsonl:
                    fpath.unlink(missing_ok=True)

    log(
        "RESURRECT",
        f"Selesai ({len(picked_files)} file): {len(resurrected)} domain bangkit lagi dari {len(sample_map)} dicek!",
    )
    return resurrected


def compact_and_stats(do_compact: bool = False, dry: bool = False) -> None:
    if not ARCHIVE_DIR.exists():
        log("ARCHIVE", "Folder Archive/ belum ada.")
        return
    files = sorted(
        list(ARCHIVE_DIR.glob("*.jsonl.gz"))
        + list(ARCHIVE_DIR.glob("*.jsonl"))
        + list(ARCHIVE_DIR.glob("*.txt.gz"))
    )
    if not files:
        log("ARCHIVE", "Belum ada file arsip di Archive/.")
        return

    allowlist = _load_allowlist()
    ts = int(now())
    total_doms = 0
    total_kb = 0.0

    for fpath in files:
        recs = read_archive_map(fpath)
        before = len(recs)
        for d in [dom for dom in recs if dom in allowlist]:
            del recs[d]
        if do_compact and not dry:
            cat = _safe_cat(
                fpath.name.removesuffix(".jsonl.gz")
                .removesuffix(".jsonl")
                .removesuffix(".txt.gz")
            )
            target = ARCHIVE_DIR / f"{cat}.jsonl.gz"
            write_archive_map(target, recs, dry=False)
            if target != fpath:
                fpath.unlink(missing_ok=True)
            fpath = target

        size_kb = (fpath.stat().st_size / 1024.0) if fpath.exists() else 0.0
        total_doms += len(recs)
        total_kb += size_kb
        eligible = sum(
            1
            for r in recs.values()
            if _is_eligible_for_resurrect(
                r, ts, DEFAULT_DORMANT_DAYS, DEFAULT_MAX_RESURRECT
            )
        )
        log(
            "ARCHIVE",
            f"  {fpath.name}: {len(recs)} domain (siap_cek={eligible}, -{before - len(recs)} allowlist) | {size_kb:.1f} KB",
        )

    log("ARCHIVE", f"Total: {total_doms} domain | Ukuran total: {total_kb:.1f} KB")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Archive Manager & Zombie Resurrector")
    ap.add_argument(
        "--max-cek",
        type=int,
        default=500,
        help="Jumlah sampel domain arsip yang dicek DNS-nya",
    )
    ap.add_argument(
        "--timeout", type=int, default=45, help="Batas waktu pengecekan DNS (detik)"
    )
    ap.add_argument(
        "--min-dormant-days",
        type=int,
        default=DEFAULT_DORMANT_DAYS,
        help="Masa tunggu minimum sebelum dicek ulang",
    )
    ap.add_argument(
        "--max-resurrect",
        type=int,
        default=DEFAULT_MAX_RESURRECT,
        help="Batas maksimal kebangkitan domain",
    )
    ap.add_argument(
        "--stats", action="store_true", help="Tampilkan statistik seluruh file Archive/"
    )
    ap.add_argument(
        "--compact",
        action="store_true",
        help="Rapikan, deduplikasi, dan kompres ulang Archive/ secara deterministik",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.stats or args.compact:
        compact_and_stats(do_compact=args.compact, dry=args.dry_run)
        if not args.compact and args.stats:
            return 0

    woke = bangkitkan_arsip(
        max_cek=args.max_cek,
        timeout_sec=args.timeout,
        dry=args.dry_run,
        min_dormant_days=args.min_dormant_days,
        max_resurrect=args.max_resurrect,
    )
    for dom, cat, _ in woke[:30]:
        log("RESURRECT", f"  + [{cat}] {dom}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
