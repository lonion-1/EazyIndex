# Copyright (c) 2026 EazyWhale
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lightweight Single-Pass HTTP Fetcher dengan proteksi SSRF & circuit-breaker per host."""
from __future__ import annotations

import gzip
import http.client
import ipaddress
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass

UA_POOL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)


@dataclass
class FetchResponse:
    ok: bool
    kind: str  # "ok" | "gone" | "blocked" | "transient" | "ssrf" | "skipped"
    status: int = 0
    url: str = ""
    text: str = ""


def _is_private_or_reserved_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if not h or h in ("localhost", "ip6-localhost", "ip6-loopback"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(h, None, proto=socket.IPPROTO_TCP)
        for info in infos[:4]:
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                return True
    except Exception:
        return False
    return False


def _decompress(raw: bytes, encoding: str) -> bytes:
    enc = (encoding or "").lower()
    if "gzip" in enc:
        try:
            return gzip.decompress(raw)
        except Exception:
            return raw
    if "deflate" in enc:
        try:
            return zlib.decompress(raw)
        except Exception:
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
            except Exception:
                return raw
    return raw


class SimpleHttpClient:
    """HTTP client mandiri tanpa dependensi eksternal, dengan circuit breaker per host."""

    def __init__(self, dry: bool = False):
        self.dry = dry
        self._lock = threading.Lock()
        self._host_fails: dict[str, int] = {}
        self._tripped: set[str] = set()
        self._ssl_lax = ssl.create_default_context()
        self._ssl_lax.check_hostname = False
        self._ssl_lax.verify_mode = ssl.CERT_NONE

    def is_tripped(self, host: str) -> bool:
        with self._lock:
            return host.lower() in self._tripped

    def _record_host(self, host: str, ok: bool) -> None:
        h = host.lower()
        with self._lock:
            if ok:
                self._host_fails[h] = 0
            else:
                self._host_fails[h] = self._host_fails.get(h, 0) + 1
                if self._host_fails[h] >= 3:
                    self._tripped.add(h)

    def get(
        self,
        url: str,
        timeout: float = 10.0,
        max_bytes: int = 350_000,
        deadline_ts: float | None = None,
    ) -> FetchResponse:
        if deadline_ts is not None and time.monotonic() >= deadline_ts - 0.5:
            return FetchResponse(False, "skipped", 0, url, "")

        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return FetchResponse(False, "ssrf", 0, url, "")

        host = p.hostname.lower()
        if self.is_tripped(host):
            return FetchResponse(False, "skipped", 0, url, "")

        if _is_private_or_reserved_host(host):
            return FetchResponse(False, "ssrf", 0, url, "")

        eff_timeout = timeout
        if deadline_ts is not None:
            eff_timeout = max(2.0, min(timeout, deadline_ts - time.monotonic()))

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": random.choice(UA_POOL),
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "close",
            },
        )

        for ctx_ssl in (None, self._ssl_lax):
            try:
                opener = (
                    urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx_ssl))
                    if ctx_ssl is not None
                    else urllib.request.build_opener()
                )
                with opener.open(req, timeout=eff_timeout) as resp:
                    final_url = resp.geturl() or url
                    final_host = (urllib.parse.urlparse(final_url).hostname or "").lower()
                    if final_host and final_host != host and _is_private_or_reserved_host(final_host):
                        return FetchResponse(False, "ssrf", 0, final_url, "")

                    status = getattr(resp, "status", 200)
                    raw = resp.read(max_bytes)
                    raw = _decompress(raw, resp.headers.get("Content-Encoding", ""))
                    charset = resp.headers.get_content_charset() or "utf-8"
                    text = raw.decode(charset, errors="ignore")
                    self._record_host(host, True)
                    return FetchResponse(True, "ok", status, final_url, text)
            except ssl.SSLError:
                if ctx_ssl is None:
                    continue
                self._record_host(host, False)
                return FetchResponse(False, "transient", 0, url, "")
            except urllib.error.HTTPError as e:
                code = int(getattr(e, "code", 0))
                if code in (404, 410):
                    self._record_host(host, False)
                    return FetchResponse(False, "gone", code, url, "")
                if code in (401, 403, 429, 451):
                    self._record_host(host, False)
                    return FetchResponse(False, "blocked", code, url, "")
                self._record_host(host, False)
                return FetchResponse(False, "transient", code, url, "")
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException) as e:
                reason_str = str(getattr(e, "reason", e)).lower()
                if "name or service not known" in reason_str or "nodename nor servname" in reason_str or "getaddrinfo" in reason_str:
                    self._record_host(host, False)
                    return FetchResponse(False, "gone", 0, url, "")
                self._record_host(host, False)
                return FetchResponse(False, "transient", 0, url, "")

        return FetchResponse(False, "transient", 0, url, "")
