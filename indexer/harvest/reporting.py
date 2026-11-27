# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub Step Summary writer."""
from __future__ import annotations

import os


def write_summary(stats: dict, dry: bool) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                f"## 🔍 Suggest Harvester{' (dry-run)' if dry else ''}\n"
                f"- Request: {stats['requests']} | "
                f"Diterima ke feed: +{stats['accepted']} | "
                f"Total pool: {stats['pool']}\n\n"
            )
    except OSError:
        pass
