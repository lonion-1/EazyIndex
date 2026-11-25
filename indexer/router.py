from __future__ import annotations

import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EngineHealth:
    fails: int = 0
    cool_until: int = 0
    ok_runs: int = 0
    last_reason: str = ""


class EngineRouter:
    """Router multi-gelombang dengan circuit-breaker, bias kategori, dan anti-tabrak WAF."""

    def __init__(self, cfg: dict, state_path: Path):
        self.cfg = cfg
        self.eng = cfg["engines"]
        self.templates: dict[str, str] = self.eng["templates"]
        self.fallback: list[str] = list(self.eng["fallback"])
        self.profiles: dict[str, dict] = self.eng.get("profiles", {})
        self.waves: dict[str, dict] = self.eng.get("waves", {})
        self.cat_bias: dict[str, list[str]] = self.eng.get("category_bias", {})
        self.pagination: dict[str, dict] = self.eng.get("pagination", {})
        self.waf_patterns: tuple[str, ...] = tuple(
            p.lower()
            for p in (self.eng.get("waf_signatures") or {}).get("patterns", [])
        )
        self.state_path = state_path
        self.health: dict[str, EngineHealth] = self._load_health()

    def _load_health(self) -> dict[str, EngineHealth]:
        if not self.state_path.exists():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {k: EngineHealth(**v) for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            return {}

    def save_health(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        payload = {
            k: {
                "fails": v.fails,
                "cool_until": v.cool_until,
                "ok_runs": v.ok_runs,
                "last_reason": v.last_reason,
            }
            for k, v in sorted(self.health.items())
        }
        tmp.write_text(
            json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path := self.state_path)

    def wave_policy(self, name: str) -> dict:
        for wcfg in self.waves.values():
            if name in wcfg.get("engines", []):
                return wcfg
        return {"timeout": 15, "sleep_mult": 1.0}

    def plan_for(self, category: str, now_ts: int | None = None) -> list[str]:
        """Prioritaskan engine sesuai kategori -> lanjut ke fallback gelombang -> lewati yang cooldown."""
        now = int(now_ts or time.time())
        preferred = [e for e in self.cat_bias.get(category, []) if e in self.templates]
        ordered = list(dict.fromkeys(preferred + self.fallback))
        active = [
            name
            for name in ordered
            if not (self.health.get(name) and self.health[name].cool_until > now)
        ]
        max_try = int(self.eng.get("max_engines_per_query", 5))
        return active[:max_try]

    def build_url(self, name: str, query: str, page_idx: int = 0) -> str:
        tpl = self.templates[name]
        pg = self.pagination.get(name, {})
        step = int(pg.get("step", 10))
        start_offset = int(pg.get("start_offset", 0))
        prof = self.profiles.get(name, {})
        lang = str(prof.get("lang", "id"))
        offset = start_offset + (page_idx * step)
        return tpl.format(
            q=urllib.parse.quote_plus(query),
            page=page_idx,
            page1=page_idx + 1,
            offset=offset,
            offset1=offset + 1,
            lang=urllib.parse.quote_plus(lang),
        )

    def is_waf_blocked(self, html_body: str) -> bool:
        sample = (html_body or "")[:25_000].lower()
        return any(sig in sample for sig in self.waf_patterns)

    def record_ok(self, name: str) -> None:
        h = self.health.setdefault(name, EngineHealth())
        h.fails = 0
        h.cool_until = 0
        h.ok_runs += 1
        h.last_reason = "ok"

    def record_fail(self, name: str, reason: str, now_ts: int | None = None) -> None:
        now = int(now_ts or time.time())
        h = self.health.setdefault(name, EngineHealth())
        h.fails += 1
        h.last_reason = reason
        base_cool = int(self.eng.get("cooldown_base_seconds", 1800))
        max_cool = int(self.eng.get("cooldown_max_seconds", 86400))
        mult = 4 if reason in ("captcha", "waf_403", "rate_429") else 1
        h.cool_until = now + min(
            max_cool, base_cool * mult * (2 ** min(h.fails - 1, 5))
        )
