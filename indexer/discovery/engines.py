# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Router mesin pencari (SearXNG auto-discovery + HTML fallback) & pembersih redirect SERP."""

from __future__ import annotations

import base64
import html
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..text_utils import registrable_domain, strip_tags

SEARX_SPACE_URL = "https://searx.space/data/instances.json"
PROBE_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"

RE_A = re.compile(
    r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', re.IGNORECASE
)
RE_TAG = re.compile(r"<[^>]+>")
RE_SNIPPET_BOUNDARY = re.compile(r"<a\b|</(?:article|li|tr|section|dd)>", re.IGNORECASE)

ENGINE_NOISE_DOMAINS = frozenset(
    {
        "duckduckgo.com",
        "duck.com",
        "spreadprivacy.com",
        "bing.com",
        "microsoft.com",
        "msn.com",
        "live.com",
        "yahoo.com",
        "aol.com",
        "mojeek.com",
        "brave.com",
        "qwant.com",
        "ecosia.org",
        "metager.org",
        "yandex.com",
        "yandex.ru",
        "google.com",
        "gstatic.com",
        "googleapis.com",
        "googleusercontent.com",
        "startpage.com",
        "searx.space",
        "w3.org",
        "schema.org",
        "cloudflare.com",
    }
)

TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "yclid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "si",
    }
)

STATIC_ASSET_EXTS = frozenset(
    {
        ".css",
        ".js",
        ".mjs",
        ".map",
        ".ico",
        ".svg",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".avif",
        ".mp4",
        ".mp3",
        ".webm",
        ".ogg",
    }
)

JUNK_ANCHOR_TEXTS = frozenset(
    {
        "next",
        "previous",
        "prev",
        "cached",
        "similar",
        "more",
        "images",
        "videos",
        "news",
        "maps",
        "settings",
        "privacy",
        "privacy policy",
        "terms",
        "terms of service",
        "cookie policy",
        "help",
        "feedback",
        "about",
        "about us",
        "contact",
        "advertise",
        "sign in",
        "log in",
        "login",
        "register",
        "sign up",
        "berikutnya",
        "sebelumnya",
        "tembolok",
        "mirip",
        "masuk",
        "daftar",
        "tentang",
        "bantuan",
        "kebijakan privasi",
        "syarat dan ketentuan",
    }
)

BLOCK_SIGNATURES = (
    "smartcaptcha",
    "cf-browser-verification",
    "challenge-platform",
    "just a moment...",
    "are you a robot",
    "unusual traffic",
    "automated queries",
    "anomaly-modal",
    "anomaly.js",
    "g-recaptcha",
    "h-captcha",
    "cf-turnstile",
    "/sorry/index",
    "verify you are human",
    "detected unusual traffic",
    "too many requests",
    "rate limit exceeded",
    "access denied",
)

_DISCOVERY_LOCK = threading.Lock()
_DISCOVERY_RUNNING = False


@dataclass
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    engine: str = ""


def _http_get_raw(url: str, timeout: float = 5.0) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": PROBE_UA,
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if not (200 <= resp.status < 300):
            return ""
        return resp.read(512_000).decode("utf-8", errors="ignore")


def _probe_searxng_json(base_url: str, timeout: float = 3.5) -> str | None:
    base = base_url.rstrip("/")
    test_url = f"{base}/search?q=wikipedia&format=json&pageno=1&safesearch=0&categories=general"
    try:
        raw = _http_get_raw(test_url, timeout=timeout)
        if not raw:
            return None
        data = json.loads(raw)
        if isinstance(data.get("results"), list) and data["results"]:
            return base
    except Exception:
        pass
    return None


def _refresh_searx_cache_bg(
    target_count: int,
    probe_limit: int,
    max_latency: float,
    cache_path: str,
    pool_ref: EnginePool | None = None,
) -> None:
    global _DISCOVERY_RUNNING
    try:
        try:
            raw_dir = _http_get_raw(SEARX_SPACE_URL, timeout=8.0)
            directory = json.loads(raw_dir).get("instances", {})
        except Exception:
            return

        candidates: list[tuple[float, str]] = []
        for raw_url, meta in directory.items():
            if not isinstance(meta, dict) or not raw_url.startswith("https://"):
                continue
            if meta.get("network_type") != "normal":
                continue
            http_info = meta.get("http") or {}
            if http_info.get("status_code") != 200 or http_info.get("error"):
                continue
            timing = meta.get("timing") or {}
            lat = (
                ((timing.get("search") or {}).get("all") or {}).get("value")
                or ((timing.get("initial") or {}).get("all") or {}).get("value")
                or 9.9
            )
            try:
                lat = float(lat)
            except (TypeError, ValueError):
                lat = 9.9
            if lat <= max_latency:
                candidates.append((lat, raw_url.rstrip("/")))

        candidates.sort(key=lambda x: x[0])
        to_probe = [u for _, u in candidates[:probe_limit]]
        if not to_probe:
            return

        verified: list[str] = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_probe_searxng_json, u, 3.5): u for u in to_probe}
            for fut in as_completed(futs):
                ok = fut.result()
                if ok:
                    verified.append(ok)
                    if len(verified) >= target_count:
                        for pending in futs:
                            pending.cancel()
                        break

        if verified:
            cfile = Path(cache_path)
            try:
                cfile.parent.mkdir(parents=True, exist_ok=True)
                cfile.write_text(
                    json.dumps({"ts": time.time(), "urls": verified}), encoding="utf-8"
                )
            except OSError:
                pass
            if pool_ref is not None:
                pool_ref.inject_engines([SearxngEngine(u) for u in verified])
    finally:
        with _DISCOVERY_LOCK:
            _DISCOVERY_RUNNING = False


def _unwrap_once(url: str) -> str:
    u = html.unescape(url).strip()
    if not u:
        return ""
    if u.startswith("//"):
        u = "https:" + u

    p = urllib.parse.urlparse(u)
    host = p.netloc.lower()
    q = urllib.parse.parse_qs(p.query)

    if not host:
        if p.path.startswith("/l/") and "uddg" in q:
            return urllib.parse.unquote(q["uddg"][0])
        if p.path == "/url":
            for key in ("q", "url"):
                if key in q and q[key][0].startswith(("http://", "https://")):
                    return urllib.parse.unquote(q[key][0])
        if p.path.startswith("/ck/") and q.get("u", [""])[0].startswith("a1"):
            b = q["u"][0][2:]
            b += "=" * (-len(b) % 4)
            try:
                return base64.urlsafe_b64decode(b).decode("utf-8", "ignore")
            except Exception:
                return ""
        return u

    if host.endswith("duckduckgo.com") and p.path.startswith("/l/") and "uddg" in q:
        return urllib.parse.unquote(q["uddg"][0])
    if (
        host.endswith("bing.com")
        and p.path.startswith("/ck/")
        and q.get("u", [""])[0].startswith("a1")
    ):
        b = q["u"][0][2:]
        b += "=" * (-len(b) % 4)
        try:
            return base64.urlsafe_b64decode(b).decode("utf-8", "ignore")
        except Exception:
            return ""
    if host.endswith("google.com") and p.path == "/url":
        for key in ("q", "url"):
            if key in q:
                return q[key][0]
    return u


def decode_serp_url(url: str) -> str:
    """Buka pembungkus redirect SERP (DDG/Bing/Google) & buang parameter tracking."""
    u = url
    for _ in range(3):
        nxt = _unwrap_once(u)
        if not nxt or nxt == u:
            u = nxt
            break
        u = nxt

    if not u.startswith(("http://", "https://")):
        return ""
    try:
        p = urllib.parse.urlparse(u)
    except ValueError:
        return ""

    host = p.netloc.lower()
    if host.endswith(":80"):
        host = host[:-3]
    elif host.endswith(":443"):
        host = host[:-4]
    if not host or "." not in host:
        return ""

    if p.query:
        pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        clean_query = urllib.parse.urlencode(
            [(k, v) for k, v in pairs if k.lower() not in TRACKING_PARAMS],
            doseq=True,
        )
    else:
        clean_query = ""
    return urllib.parse.urlunparse(
        (p.scheme.lower(), host, p.path or "/", "", clean_query, "")
    )


def detect_serp_block(raw_html: str) -> bool:
    sample = raw_html[:24000].lower()
    return any(sig in sample for sig in BLOCK_SIGNATURES)


def _extract_trailing_snippet(raw_html: str, anchor_end: int, title: str) -> str:
    chunk = raw_html[anchor_end : anchor_end + 800]
    boundary = RE_SNIPPET_BOUNDARY.search(chunk)
    if boundary:
        chunk = chunk[: boundary.start()]
    txt = strip_tags(chunk)
    if not txt or txt.lower() == title.lower():
        return ""
    return txt[:240]


class SearxngEngine:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.name = "searxng@" + urllib.parse.urlparse(self.base).netloc

    def search(
        self, http, q: str, page: int, deadline_ts: float, lang: str
    ) -> tuple[str, list[SearchHit]]:
        url = (
            f"{self.base}/search?q={urllib.parse.quote_plus(q)}&format=json&pageno={page}"
            f"&language={urllib.parse.quote_plus(lang or 'id')}&safesearch=0&categories=general"
        )
        r = http.get(url, timeout=8.0, max_bytes=400_000, deadline_ts=deadline_ts)
        if not r.ok:
            return r.kind, []
        try:
            data = json.loads(r.text)
        except ValueError:
            return "blocked", []

        results = data.get("results")
        if not isinstance(results, list):
            return "blocked", []

        unresponsive = data.get("unresponsive_engines")
        if not results and isinstance(unresponsive, list) and len(unresponsive) >= 2:
            return "blocked", []

        seen: set[str] = set()
        hits: list[SearchHit] = []
        for x in results:
            if not isinstance(x, dict) or not x.get("url"):
                continue
            u = decode_serp_url(x["url"])
            if not u or u in seen:
                continue
            seen.add(u)
            src = x.get("engine") or self.name
            hits.append(
                SearchHit(
                    url=u,
                    title=strip_tags(x.get("title", "") or "")[:180],
                    snippet=strip_tags(x.get("content", "") or "")[:280],
                    engine=f"{self.name}:{src}" if src != self.name else self.name,
                )
            )
        return "ok", hits


class HtmlSearchEngine:
    def __init__(self, name: str, template: str, multi_tlds: frozenset[str]):
        self.name = name
        self.template = template
        self.multi_tlds = multi_tlds
        host = urllib.parse.urlparse(template).netloc.lower()
        self.root = registrable_domain(host, multi_tlds)
        self.supports_paging = any(
            tok in template for tok in ("{page}", "{page1}", "{offset}", "{offset1}")
        )

    def _build_url(self, q: str, page: int, lang: str) -> str:
        offset = (page - 1) * 10
        return (
            self.template.replace("{q}", urllib.parse.quote_plus(q))
            .replace("{lang}", urllib.parse.quote_plus(lang or "id"))
            .replace("{page}", str(page))
            .replace("{page1}", str(page))
            .replace("{offset}", str(offset))
            .replace("{offset1}", str(offset + 1))
        )

    def search(
        self, http, q: str, page: int, deadline_ts: float, lang: str
    ) -> tuple[str, list[SearchHit]]:
        if page > 1 and not self.supports_paging:
            return "ok", []

        r = http.get(
            self._build_url(q, page, lang),
            timeout=9.0,
            max_bytes=450_000,
            deadline_ts=deadline_ts,
        )
        if not r.ok:
            return r.kind, []
        if detect_serp_block(r.text):
            return "blocked", []

        seen: set[str] = set()
        hits: list[SearchHit] = []
        for m in RE_A.finditer(r.text):
            href, inner = m.group(1), m.group(2)
            u = decode_serp_url(href)
            if not u or u in seen:
                continue

            p = urllib.parse.urlparse(u)
            reg = registrable_domain(p.netloc.lower(), self.multi_tlds)
            if not reg or reg == self.root or reg in ENGINE_NOISE_DOMAINS:
                continue

            path_lower = p.path.lower()
            if any(path_lower.endswith(ext) for ext in STATIC_ASSET_EXTS):
                continue

            title = strip_tags(inner)[:180]
            if (
                len(title) < 3
                or title.isdigit()
                or title.lower() in JUNK_ANCHOR_TEXTS
                or not any(ch.isalnum() for ch in title)
            ):
                continue

            seen.add(u)
            snippet = _extract_trailing_snippet(r.text, m.end(), title)
            hits.append(
                SearchHit(url=u, title=title, snippet=snippet, engine=self.name)
            )

        return "ok", hits


class EnginePool:
    """Zero-sleep round-robin search router."""

    def __init__(self, engines: list, soft_gap: float = 0.8, max_empty_tries: int = 3):
        self._lock = threading.Lock()
        self.engines = list(engines)
        self.soft_gap = soft_gap
        self.max_empty_tries = max(1, max_empty_tries)
        self.fails: dict[str, int] = {e.name: 0 for e in engines}
        self.empty_streak: dict[str, int] = {e.name: 0 for e in engines}
        self.cooldown_until: dict[str, float] = {e.name: 0.0 for e in engines}
        self.last_used: dict[str, float] = {e.name: 0.0 for e in engines}
        self.turn = 0

    def inject_engines(self, new_engines: list) -> None:
        with self._lock:
            existing = {e.name for e in self.engines}
            for e in new_engines:
                if e.name not in existing:
                    self.engines.append(e)
                    self.fails[e.name] = 0
                    self.empty_streak[e.name] = 0
                    self.cooldown_until[e.name] = 0.0
                    self.last_used[e.name] = 0.0
                    existing.add(e.name)

    def _pick_order(self) -> list:
        now = time.monotonic()
        with self._lock:
            if not self.engines:
                return []
            live = [
                e for e in self.engines if self.cooldown_until.get(e.name, 0.0) <= now
            ]
            if not live:
                soonest = min(
                    self.engines, key=lambda e: self.cooldown_until.get(e.name, 0.0)
                )
                self.cooldown_until[soonest.name] = now + 12.0
                return [soonest]
            self.turn += 1
            idx = self.turn % len(live)
            rotated = live[idx:] + live[:idx]
            ready = [
                e
                for e in rotated
                if (now - self.last_used.get(e.name, 0.0)) >= self.soft_gap
            ]
            warm = [
                e
                for e in rotated
                if (now - self.last_used.get(e.name, 0.0)) < self.soft_gap
            ]
            return ready + warm

    def _record_failure(self, name: str, kind: str) -> None:
        with self._lock:
            self.fails[name] = self.fails.get(name, 0) + 1
            strikes = self.fails[name]
            if kind == "blocked":
                self.cooldown_until[name] = time.monotonic() + min(
                    600.0, 90.0 * strikes
                )
            elif strikes >= 2:
                self.cooldown_until[name] = time.monotonic() + min(
                    300.0, 20.0 * (2 ** (strikes - 2))
                )

    def search(
        self, http, q: str, page: int, deadline_ts: float, lang: str
    ) -> list[SearchHit]:
        order = self._pick_order()
        ok_empty = 0
        for e in order:
            if time.monotonic() >= deadline_ts - 0.5:
                break
            with self._lock:
                self.last_used[e.name] = time.monotonic()

            kind, hits = e.search(http, q, page, deadline_ts, lang)
            if kind == "ok":
                with self._lock:
                    self.fails[e.name] = 0
                if hits:
                    with self._lock:
                        self.empty_streak[e.name] = 0
                    return hits
                if page == 1:
                    with self._lock:
                        self.empty_streak[e.name] = self.empty_streak.get(e.name, 0) + 1
                        if self.empty_streak[e.name] >= 4:
                            self.cooldown_until[e.name] = time.monotonic() + 60.0
                            self.empty_streak[e.name] = 0
                ok_empty += 1
                if ok_empty >= self.max_empty_tries:
                    break
            else:
                self._record_failure(e.name, kind)
        return []


def build_engine_pool(
    ec: dict, multi_tlds: frozenset[str], only: set[str] | None = None
) -> EnginePool:
    global _DISCOVERY_RUNNING
    engines: list = []

    want_searx = not only or "searxng" in only
    cache_path = str(ec.get("cache_path", ".cache/webindex/searx_space_verified.json"))
    cache_ttl = int(ec.get("cache_ttl", 21600))
    need_bg_refresh = False

    if want_searx:
        urls = [u.rstrip("/") for u in ec.get("searxng", []) if str(u).strip()]
        urls += [
            u.strip().rstrip("/")
            for u in os.environ.get("SEARXNG_URL", "").split(",")
            if u.strip()
        ]

        if bool(ec.get("auto_discover", True)):
            cfile = Path(cache_path)
            if cfile.exists():
                try:
                    cached = json.loads(cfile.read_text(encoding="utf-8"))
                    urls.extend(u for u in cached.get("urls", []) if isinstance(u, str))
                    if (time.time() - float(cached.get("ts", 0))) >= cache_ttl:
                        need_bg_refresh = True
                except Exception:
                    need_bg_refresh = True
            else:
                need_bg_refresh = True

        for u in dict.fromkeys(urls):
            engines.append(SearxngEngine(u))

    tmpl = ec.get("templates") or {}
    for name in dict.fromkeys(ec.get("fallback", [])):
        if name in tmpl:
            engines.append(HtmlSearchEngine(name, tmpl[name], multi_tlds))

    if only:
        engines = [e for e in engines if e.name.split("@")[0] in only]

    pool = EnginePool(
        engines,
        soft_gap=float(ec.get("soft_gap", 0.8)),
        max_empty_tries=int(ec.get("max_empty_tries", 3)),
    )

    if want_searx and bool(ec.get("auto_discover", True)) and need_bg_refresh:
        with _DISCOVERY_LOCK:
            if not _DISCOVERY_RUNNING:
                _DISCOVERY_RUNNING = True
                t = threading.Thread(
                    target=_refresh_searx_cache_bg,
                    args=(
                        int(ec.get("discover_count", 6)),
                        int(ec.get("probe_limit", 24)),
                        float(ec.get("max_latency", 2.5)),
                        cache_path,
                        pool,
                    ),
                    daemon=True,
                )
                t.start()

    return pool
