# Copyright (c) 2026 EazyWhale
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Utilitas normalisasi teks, deobfuscation (homoglyph/leet/zero-width), dan ekstraksi domain."""

from __future__ import annotations

import html
import re
import unicodedata
import urllib.parse

RE_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
RE_TAG = re.compile(r"<[^>]+>")
RE_WS = re.compile(r"\s+")
RE_DOMAIN_VALID = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$"
)

LEET_TRANS = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "8": "b",
        "@": "a",
        "$": "s",
    }
)


def norm_space(text: str) -> str:
    if not text:
        return ""
    s = html.unescape(text)
    s = RE_ZERO_WIDTH.sub("", s)
    s = unicodedata.normalize("NFKC", s)
    return RE_WS.sub(" ", s).strip()


def strip_tags(raw_html: str) -> str:
    if not raw_html:
        return ""
    return norm_space(RE_TAG.sub(" ", raw_html))


def deobfuscate(text: str, max_len: int = 4000) -> list[str]:
    """Hasilkan varian teks bersih (NFKC + de-leet + tanpa pemisah aneh) untuk mendeteksi kamuflase."""
    base = norm_space(text)[:max_len].lower()
    if not base:
        return []
    variants = [base]
    deleet = base.translate(LEET_TRANS)
    if deleet != base:
        variants.append(deleet)
    collapsed = re.sub(r"(?<=[a-z0-9])[._*-]+(?=[a-z0-9])", "", deleet)
    if collapsed != deleet and len(collapsed) >= 4:
        variants.append(collapsed)
    return list(dict.fromkeys(variants))


def registrable_domain(host: str, multi_tlds: frozenset[str]) -> str:
    h = (host or "").strip().lower().rstrip(".")
    if ":" in h:
        h = h.split(":", 1)[0]
    h = h.removeprefix("www.")
    if not h or "." not in h or not RE_DOMAIN_VALID.match(h):
        return ""
    parts = h.split(".")
    if len(parts) <= 2:
        return h
    tail2 = ".".join(parts[-2:])
    if tail2 in multi_tlds and len(parts) >= 3:
        return ".".join(parts[-3:])
    return tail2


def is_subdomain_of(domain: str, parents: set[str] | frozenset[str]) -> bool:
    if not domain:
        return False
    if domain in parents:
        return True
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in parents:
            return True
    return False


def extract_safe_domain(
    host_or_url: str,
    multi_tlds: frozenset[str],
    allowlist: set[str] | frozenset[str],
    institutional_tlds: tuple[str, ...] = (),
) -> str:
    """Ekstrak registrable domain yang valid dan bukan milik allowlist/TLD institusi resmi."""
    raw = (host_or_url or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        host = urllib.parse.urlparse(raw).hostname or ""
    else:
        host = raw.split("/", 1)[0].split("?", 1)[0]
    dom = registrable_domain(host, multi_tlds)
    if not dom:
        return ""
    if is_subdomain_of(dom, allowlist):
        return ""
    if any(dom.endswith(tld) for tld in institutional_tlds):
        return ""
    return dom
