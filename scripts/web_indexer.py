#!/usr/bin/env python3
# Copyright (c) 2026 EazyWhale
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entrypoint CLI untuk Front-Gate Web Indexer (Tier 1)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer.main import main

if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
