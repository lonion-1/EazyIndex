# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: MIT
"""Regex statis untuk deteksi kontak & pembayaran di snippet SERP."""

from __future__ import annotations

import re

RE_WA = re.compile(
    r"(?:wa\.me/|api\.whatsapp\.com/send\?[^\s>]*?phone=)\+?(\d{8,15})", re.IGNORECASE
)
RE_TG = re.compile(
    r"(?:t\.me|telegram\.me)/(?!share|joinchat|s/|\+)([A-Za-z0-9_]{4,32})",
    re.IGNORECASE,
)
RE_PAYMENT = re.compile(
    r"(?:transfer|rekening|no\.?\s*rek|norek|a\.?n\.?)[^.\n]{0,50}?\b\d{8,16}\b|"
    r"\b(?:dana|ovo|gopay|shopeepay|linkaja)\b[^.\n]{0,30}\b08\d{8,11}\b",
    re.IGNORECASE,
)
