# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shim untuk backward compatibility. Gunakan `from .serp import ...` untuk kode baru."""

from .serp import DefaultPolicy, SerpHit, SerpPolicy, evaluate_serp_hit

__all__ = ["evaluate_serp_hit", "SerpHit", "SerpPolicy", "DefaultPolicy"]
