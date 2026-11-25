# Copyright (c) 2026 EazyWhale
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whole-Page 5-Zone DOM Parser:
Membedah HTML menjadi 5 zona terpisah (Head/JSON-LD, Nav, Main, Outbound/Ads, Footer)
sehingga situs ilegal yang menyamarkan <header>/<title> tetap terdeteksi dari badan, navigasi, form, & footer.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.parse

from ..models import PageZones
from ..text_utils import norm_space, strip_tags

RE_TITLE = re.compile(r"<title[^>]*>([\s\S]*?)</title>", re.IGNORECASE)
RE_META = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
RE_META_NAME = re.compile(
    r'(?:name|property)=["\'](?:description|keywords|og:title|og:description|og:site_name|author|publisher)["\']',
    re.IGNORECASE,
)
RE_META_CONTENT = re.compile(r'content=["\']([^"\']*)["\']', re.IGNORECASE)
RE_H12 = re.compile(r"<h[12][^>]*>([\s\S]*?)</h[12]>", re.IGNORECASE)

RE_JSONLD = re.compile(
    r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
    re.IGNORECASE,
)
RE_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>([\s\S]*?)</script>", re.IGNORECASE)
RE_STYLE_BLOCK = re.compile(r"<style\b[^>]*>[\s\S]*?</style>", re.IGNORECASE)
RE_NAV_BLOCK = re.compile(
    r"<(?:nav|header)\b[^>]*>([\s\S]*?)</(?:nav|header)>", re.IGNORECASE
)
RE_FOOTER_BLOCK = re.compile(r"<footer\b[^>]*>([\s\S]*?)</footer>", re.IGNORECASE)
RE_MAIN_BLOCK = re.compile(
    r"<(?:main|article)\b[^>]*>([\s\S]*?)</(?:main|article)>", re.IGNORECASE
)
RE_FORM_BLOCK = re.compile(r"<form\b[^>]*>([\s\S]*?)</form>", re.IGNORECASE)
RE_PASSWORD_INPUT = re.compile(r'<input\b[^>]*type=["\']?password', re.IGNORECASE)
RE_IFRAME_SRC = re.compile(r'<iframe\b[^>]*?\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)

RE_ANCHOR = re.compile(r"<a\b([^>]*)>([\s\S]*?)</a>", re.IGNORECASE)
RE_HREF_ATTR = re.compile(r'href=["\']([^"\'#\s]+)["\']', re.IGNORECASE)
RE_ATTR_TXT = re.compile(r'(?:alt|title|aria-label)=["\']([^"\']+)["\']', re.IGNORECASE)
RE_IMG_SRC = re.compile(r'<img\b[^>]*?\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)


def _collect_jsonld_types(obj, out: set[str]) -> None:
    if isinstance(obj, dict):
        t = obj.get("@type")
        if isinstance(t, str) and t.strip():
            out.add(t.strip())
        elif isinstance(t, list):
            for item in t:
                if isinstance(item, str) and item.strip():
                    out.add(item.strip())
        for v in obj.values():
            _collect_jsonld_types(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_jsonld_types(item, out)


def parse_html_zones(raw_html: str, final_url: str) -> PageZones:
    raw = (raw_html or "")[:400_000]
    base_host = (
        (urllib.parse.urlparse(final_url).hostname or "").lower().removeprefix("www.")
    )

    # 1. Ekstrak JSON-LD sebelum menghapus tag <script>
    jsonld_types: set[str] = set()
    for m in RE_JSONLD.finditer(raw):
        blob = m.group(1).strip()
        if not blob:
            continue
        try:
            parsed = json.loads(blob)
            _collect_jsonld_types(parsed, jsonld_types)
        except Exception:
            for tm in re.finditer(r'"@type"\s*:\s*"([A-Za-z]+)"', blob):
                jsonld_types.add(tm.group(1))

    # 2. Kumpulkan jejak skrip iklan / tracker / redirect inline sebelum dibersihkan
    script_chunks: list[str] = []
    for m in RE_SCRIPT_BLOCK.finditer(raw):
        s_body = m.group(1)
        if s_body and len(s_body) < 8000:
            script_chunks.append(s_body)
    script_markers = norm_space(" ".join(script_chunks))[:6000].lower()

    # Bersihkan <script> dan <style> agar teks konten bersih dari kode JS/CSS
    clean_dom = RE_STYLE_BLOCK.sub(" ", RE_SCRIPT_BLOCK.sub(" ", raw))

    # 3. Zona Head (Title, Meta, H1/H2)
    t_m = RE_TITLE.search(clean_dom)
    title = strip_tags(t_m.group(1))[:220] if t_m else ""

    metas: list[str] = []
    for tag in RE_META.findall(clean_dom[:120_000]):
        if RE_META_NAME.search(tag):
            cm = RE_META_CONTENT.search(tag)
            if cm:
                metas.append(cm.group(1))
    meta_txt = strip_tags(" ".join(metas))[:600]
    h12_txt = strip_tags(" ".join(RE_H12.findall(clean_dom[:200_000])[:8]))[:400]
    head_text = norm_space(f"{title} {meta_txt} {h12_txt}")

    # 4. Zona Navigasi (<nav> / <header>)
    nav_blocks = RE_NAV_BLOCK.findall(clean_dom[:200_000])
    nav_html = " ".join(nav_blocks)
    nav_text = strip_tags(nav_html)[:2500]
    nav_links: list[str] = []
    for href in RE_HREF_ATTR.findall(nav_html)[:150]:
        absu = urllib.parse.urljoin(final_url, html.unescape(href))
        p = urllib.parse.urlparse(absu)
        if p.path and p.path != "/":
            nav_links.append(p.path.lower())

    # 5. Zona Footer (<footer> atau 20% bagian bawah dokumen jika tag <footer> tidak dipakai)
    footer_blocks = RE_FOOTER_BLOCK.findall(clean_dom)
    if footer_blocks:
        footer_text = strip_tags(" ".join(footer_blocks))[:3000]
    else:
        tail_slice = clean_dom[int(len(clean_dom) * 0.80) :]
        footer_text = strip_tags(tail_slice)[:2500]

    # 6. Zona Konten Utama (<main>/<article> atau seluruh tubuh halaman)
    main_blocks = RE_MAIN_BLOCK.findall(clean_dom)
    if main_blocks:
        main_text = strip_tags(" ".join(main_blocks))[:16000]
    else:
        body_no_nav = RE_FOOTER_BLOCK.sub(" ", RE_NAV_BLOCK.sub(" ", clean_dom))
        main_text = strip_tags(body_no_nav)[:16000]

    full_text = strip_tags(clean_dom)[:22000]

    # 7. Form & Iframes
    has_password = bool(RE_PASSWORD_INPUT.search(clean_dom))
    form_text = strip_tags(" ".join(RE_FORM_BLOCK.findall(clean_dom)))[:3000]
    iframes = [
        urllib.parse.urljoin(final_url, html.unescape(src.strip()))
        for src in RE_IFRAME_SRC.findall(clean_dom)[:30]
        if src.strip()
    ]

    # 8. Ekstraksi seluruh tautan internal & eksternal (termasuk banner gambar)
    internal_links: list[str] = []
    outbound_links: list[tuple[str, str, bool]] = []
    seen_int: set[str] = set()

    for attrs, inner in RE_ANCHOR.findall(clean_dom)[:1500]:
        hm = RE_HREF_ATTR.search(attrs)
        if not hm:
            continue
        absu = urllib.parse.urljoin(final_url, html.unescape(hm.group(1)))
        p = urllib.parse.urlparse(absu)
        if p.scheme not in ("http", "https") or not p.hostname:
            continue
        lhost = p.hostname.lower().removeprefix("www.")
        if lhost == base_host or (base_host and lhost.endswith("." + base_host)):
            if absu not in seen_int:
                seen_int.add(absu)
                internal_links.append(absu)
        else:
            anchor_txt = strip_tags(inner)
            alts = " ".join(RE_ATTR_TXT.findall(attrs + " " + inner))
            imgs = " ".join(
                re.sub(r"[-_./]+", " ", os.path.basename(urllib.parse.urlparse(s).path))
                for s in RE_IMG_SRC.findall(inner)
            )
            ctx = norm_space(f"{anchor_txt} {alts} {imgs}")[:250]
            outbound_links.append((absu, ctx, "<img" in inner.lower()))

    return PageZones(
        final_url=final_url,
        title=title,
        meta=meta_txt,
        head_text=head_text,
        nav_text=nav_text,
        main_text=main_text,
        footer_text=footer_text,
        full_text=full_text,
        raw_html=raw,
        jsonld_types=jsonld_types,
        nav_links=list(dict.fromkeys(nav_links)),
        internal_links=internal_links,
        outbound_links=outbound_links,
        iframes=iframes,
        has_password_input=has_password,
        form_text=form_text,
        script_markers=script_markers,
    )
