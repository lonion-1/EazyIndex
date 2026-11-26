# Copyright (c) 2026 EazyWhale
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lightweight HTTP Fetcher berbasis curl_cffi (TLS/HTTP2 impersonate) dengan:
- SSRF guard per-hop (scheme, IP privat/reserved, IPv4-mapped IPv6, redirect loop).
- DNS pinning via CURLOPT_RESOLVE (cegah TOCTOU DNS rebinding).
- Location relatif/rusak (urljoin + urldefrag + validasi hostname None).
- Truncate body di max_bytes (bukan drop) + memory spike guard via chunk slicing.
- Retry SSL sekali dengan verify=False saat handshake target gagal.
- Deteksi charset respons dari Content-Type tanpa memicu full-body apparent_encoding.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass

from curl_cffi import CurlOpt
from curl_cffi.requests import Session

try:
    from curl_cffi.requests.exceptions import RequestsError
except ImportError:  # pragma: no cover - kompat versi lama
    from curl_cffi.requests.errors import RequestsError  # type: ignore


# Kode libcurl (stabil lintas versi)
_CURLE_COULDNT_RESOLVE_HOST = 6
_CURLE_COULDNT_CONNECT = 7  # sengaja TIDAK dipetakan ke "gone"
_CURLE_OPERATION_TIMEDOUT = 28
_CURLE_SSL_CONNECT_ERROR = 35
_CURLE_TOO_MANY_REDIRECTS = 47
_CURLE_PEER_FAILED_VERIFICATION = 51

_NON_SSL_CURLE = frozenset(
    {
        _CURLE_COULDNT_RESOLVE_HOST,
        _CURLE_COULDNT_CONNECT,
        _CURLE_OPERATION_TIMEDOUT,
        _CURLE_TOO_MANY_REDIRECTS,
    }
)

_BINARY_MIME_PREFIXES = ("image/", "video/", "audio/", "font/")
_BINARY_MIME_EXACT = frozenset(
    {
        "application/pdf",
        "application/zip",
        "application/gzip",
        "application/x-gzip",
    }
)

_BINARY_MAGIC_BYTES = (
    b"\x89PNG",
    b"%PDF",
    b"PK\x03\x04",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"\x1f\x8b",
    b"Rar!",
    b"\x00\x00\x01\x00",
)

_DNS_CACHE_MAX = 2000
_DNS_CACHE_TTL = 60.0
_FAILS_RESET_THRESHOLD = 500


@dataclass
class FetchResponse:
    ok: bool
    kind: str  # "ok" | "gone" | "blocked" | "transient" | "ssrf" | "skipped"
    status: int = 0
    url: str = ""
    text: str = ""


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_private_or_reserved_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _fmt_ip_for_resolve(ip: str) -> str:
    """Bungkus IPv6 dengan kurung siku agar libcurl tidak salah parse titik dua."""
    return f"[{ip}]" if ":" in ip else ip


def _extract_charset(ct_raw: str) -> str:
    """Ambil charset dari Content-Type tanpa memicu resp.encoding saat stream=True."""
    for part in ct_raw.split(";")[1:]:
        k, sep, v = part.partition("=")
        if sep and k.strip().lower() == "charset":
            val = v.strip().strip("\"'").strip()
            if val:
                return val
    return "utf-8"


def _is_ssl_error(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code in _NON_SSL_CURLE:
        return False
    if code in (_CURLE_SSL_CONNECT_ERROR, _CURLE_PEER_FAILED_VERIFICATION):
        return True
    msg = str(exc).lower()
    return any(tok in msg for tok in ("ssl", "tls", "certificate", "handshake"))


class _DnsCache:
    """LRU + TTL cache DNS, dipakai bersama validasi IP dan pinning (cegah TOCTOU)."""

    def __init__(self, max_size: int = _DNS_CACHE_MAX, ttl: float = _DNS_CACHE_TTL):
        self._max = max_size
        self._ttl = ttl
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[float, list[str]]] = OrderedDict()

    def resolve(self, host: str) -> list[str]:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(host)
            if entry and (now - entry[0]) < self._ttl:
                self._data.move_to_end(host)
                return entry[1]

        ips: list[str] = []
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
            for info in infos[:6]:
                addr = info[4][0]
                if addr not in ips:
                    ips.append(addr)
        except OSError:
            ips = []

        # Prioritaskan IPv4 di urutan awal agar lebih stabil di runner tanpa rute IPv6
        ips.sort(key=lambda x: ":" in x)

        with self._lock:
            self._data[host] = (now, ips)
            self._data.move_to_end(host)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
        return ips


class SimpleHttpClient:
    """Klien HTTP self-contained berbasis curl_cffi dengan SSRF guard per-hop."""

    def __init__(self, dry: bool = False, impersonate: str = "chrome"):
        self.dry = dry
        self._impersonate = impersonate
        self._lock = threading.Lock()
        self._dns = _DnsCache()
        self._host_fails: dict[str, int] = {}
        self._tripped: set[str] = set()
        self.stats: dict[str, int] = {
            "ssrf_initial": 0,
            "ssrf_redirect": 0,
            "tls_fallback": 0,
            "size_guard": 0,
            "content_skip": 0,
            "redirect_loop": 0,
            "timeout_empty": 0,
        }

    def is_tripped(self, host: str) -> bool:
        with self._lock:
            return host.lower() in self._tripped

    def _record_host(self, host: str, ok: bool) -> None:
        h = host.lower()
        with self._lock:
            if ok:
                self._host_fails[h] = 0
                return
            self._host_fails[h] = self._host_fails.get(h, 0) + 1
            if self._host_fails[h] >= 3:
                self._tripped.add(h)
            if len(self._tripped) > _FAILS_RESET_THRESHOLD:
                self._tripped.clear()
            if len(self._host_fails) > _FAILS_RESET_THRESHOLD:
                self._host_fails = {k: v for k, v in self._host_fails.items() if v > 0}

    def _bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + n

    @staticmethod
    def _map_curl_error(e: BaseException, url: str) -> FetchResponse:
        code = getattr(e, "code", None)
        if code == _CURLE_COULDNT_RESOLVE_HOST:
            return FetchResponse(False, "gone", 0, url, "")
        # COULDNT_CONNECT / TIMEOUT / REDIRECT sengaja -> "transient" agar state.py
        # tidak menandai domain sebagai "dead" permanen.
        return FetchResponse(False, "transient", 0, url, "")

    def get(
        self,
        url: str,
        timeout: float = 10.0,
        max_bytes: int = 350_000,
        deadline_ts: float | None = None,
    ) -> FetchResponse:
        if deadline_ts is None:
            deadline_ts = time.monotonic() + timeout + 5.0
        if time.monotonic() >= deadline_ts - 0.5:
            return FetchResponse(False, "skipped", 0, url, "")

        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return FetchResponse(False, "ssrf", 0, url, "")

        visited_urls: set[str] = {urllib.parse.urldefrag(url)[0].lower()}
        current_url = url
        final_status = 0
        final_encoding = "utf-8"
        final_url = url

        connect_t = min(5.0, timeout)
        total_t = timeout

        for hop in range(5):
            rem = deadline_ts - time.monotonic()
            if rem <= 0.2:
                return FetchResponse(False, "transient", final_status, current_url, "")
            hop_connect = max(0.3, min(connect_t, rem))
            hop_total = max(0.5, min(total_t, rem))

            # ── Validasi host per-hop ──
            parsed = urllib.parse.urlparse(current_url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                self._bump("ssrf_redirect" if hop > 0 else "ssrf_initial")
                return FetchResponse(False, "ssrf", 0, current_url, "")

            host = parsed.hostname.rstrip(".").lower()
            port = parsed.port or (443 if parsed.scheme == "https" else 80)

            if self.is_tripped(host):
                return FetchResponse(False, "skipped", 0, current_url, "")

            # ── DNS resolve + pinning ──
            if _is_ip_literal(host):
                if _is_private_or_reserved_ip(host):
                    self._bump("ssrf_redirect" if hop > 0 else "ssrf_initial")
                    return FetchResponse(False, "ssrf", 0, current_url, "")
                resolve_opt: dict = {}
            else:
                ips = self._dns.resolve(host)
                if not ips:
                    self._record_host(host, False)
                    return FetchResponse(False, "gone", 0, current_url, "")
                if any(_is_private_or_reserved_ip(ip) for ip in ips):
                    self._bump("ssrf_redirect" if hop > 0 else "ssrf_initial")
                    return FetchResponse(False, "ssrf", 0, current_url, "")
                pinned_ips = ",".join(_fmt_ip_for_resolve(ip) for ip in ips)
                pin = f"{host}:{port}:{pinned_ips}"
                resolve_opt = {CurlOpt.RESOLVE: [pin]}

            # ── Request (retry SSL sekali) ──
            with Session(
                impersonate=self._impersonate,
                verify=True,
                trust_env=False,
                curl_options=resolve_opt,
            ) as s:
                try:
                    resp = s.get(
                        current_url,
                        allow_redirects=False,
                        stream=True,
                        timeout=(hop_connect, hop_total),
                    )
                except RequestsError as e:
                    if not _is_ssl_error(e):
                        self._record_host(host, False)
                        return self._map_curl_error(e, current_url)
                    self._bump("tls_fallback")
                    try:
                        resp = s.get(
                            current_url,
                            allow_redirects=False,
                            stream=True,
                            timeout=(hop_connect, hop_total),
                            verify=False,
                        )
                    except RequestsError as e2:
                        self._record_host(host, False)
                        return self._map_curl_error(e2, current_url)

                final_status = resp.status_code
                ct_raw = resp.headers.get("Content-Type") or ""
                final_encoding = _extract_charset(ct_raw)

                try:
                    if final_status in (404, 410):
                        self._record_host(host, False)
                        return FetchResponse(
                            False, "gone", final_status, current_url, ""
                        )
                    if final_status in (401, 403, 429, 451):
                        self._record_host(host, False)
                        return FetchResponse(
                            False, "blocked", final_status, current_url, ""
                        )
                    if final_status >= 500:
                        self._record_host(host, False)
                        return FetchResponse(
                            False, "transient", final_status, current_url, ""
                        )

                    if 300 <= final_status < 400:
                        loc = (resp.headers.get("Location") or "").strip()
                        if not loc:
                            return FetchResponse(
                                False, "transient", final_status, current_url, ""
                            )
                        next_url = urllib.parse.urldefrag(
                            urllib.parse.urljoin(current_url, loc)
                        )[0]
                        key = next_url.lower()
                        if key in visited_urls:
                            self._bump("redirect_loop")
                            return FetchResponse(
                                False, "transient", final_status, current_url, ""
                            )
                        visited_urls.add(key)
                        current_url = next_url
                        continue

                    if 200 <= final_status < 300:
                        final_url = current_url
                        self._record_host(host, True)

                        # ── Pre-check Content-Length (hanya uncompressed, toleran Value/TypeError) ──
                        has_enc = bool(resp.headers.get("Content-Encoding"))
                        if not has_enc:
                            cl_raw = resp.headers.get("Content-Length")
                            try:
                                cl = int(cl_raw) if cl_raw is not None else 0
                            except (ValueError, TypeError):
                                cl = 0
                            if cl > max_bytes * 15:
                                self._bump("size_guard")
                                return FetchResponse(
                                    False, "skipped", final_status, final_url, ""
                                )

                        # ── Blacklist Content-Type (bukan whitelist) ──
                        ct = ct_raw.lower().split(";", 1)[0].strip()
                        if ct and (
                            any(ct.startswith(p) for p in _BINARY_MIME_PREFIXES)
                            or ct in _BINARY_MIME_EXACT
                        ):
                            self._bump("content_skip")
                            return FetchResponse(
                                False, "skipped", final_status, final_url, ""
                            )

                        # ── Stream body + truncate + memory spike guard ──
                        chunks: list[bytes] = []
                        total = 0
                        timed_out_empty = False
                        binary_sniff = False

                        try:
                            for chunk in resp.iter_content(chunk_size=65536):
                                if not chunk:
                                    continue
                                if time.monotonic() > deadline_ts:
                                    if total == 0:
                                        timed_out_empty = True
                                    break
                                if (
                                    ct in ("", "application/octet-stream")
                                    and total == 0
                                ):
                                    head = chunk[:512]
                                    if b"\x00" in head or any(
                                        head.startswith(m) for m in _BINARY_MAGIC_BYTES
                                    ):
                                        binary_sniff = True
                                        break
                                allowed = max_bytes - total
                                if allowed <= 0:
                                    break
                                chunks.append(chunk[:allowed])
                                total += len(chunks[-1])
                                if total >= max_bytes:
                                    break
                        except RequestsError as e:
                            self._record_host(
                                (
                                    urllib.parse.urlparse(final_url).hostname or ""
                                ).lower(),
                                False,
                            )
                            return self._map_curl_error(e, final_url)

                        if binary_sniff:
                            self._bump("content_skip")
                            return FetchResponse(
                                False, "skipped", final_status, final_url, ""
                            )

                        if timed_out_empty:
                            self._bump("timeout_empty")
                            return FetchResponse(
                                False, "transient", final_status, final_url, ""
                            )

                        raw = b"".join(chunks)
                        try:
                            text = raw.decode(final_encoding, errors="ignore")
                        except LookupError:
                            text = raw.decode("utf-8", errors="ignore")

                        return FetchResponse(True, "ok", final_status, final_url, text)

                    return FetchResponse(
                        False, "transient", final_status, current_url, ""
                    )
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass

        return FetchResponse(False, "transient", final_status, current_url, "")
