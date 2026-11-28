# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1)
# SPDX-License-Identifier: MIT
"""Pengelola rotasi User-Agent (desktop/mobile) & profil Accept-Language dari satu hulu engines.toml."""

import random
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

type HeaderMap = dict[str, str]

_DEFAULT_DESKTOP_UAS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
)

_DEFAULT_MOBILE_UAS: tuple[str, ...] = (
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.6998.88 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.3 Mobile/15E148 Safari/604.1",
)


class DeviceKind(StrEnum):
    DESKTOP = "desktop"
    MOBILE = "mobile"


@dataclass(slots=True, frozen=True)
class EngineClientProfile:
    device: DeviceKind = DeviceKind.DESKTOP
    accept_lang: str = ""


def _clean_ua_list(raw: object, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple)):
        cleaned = tuple(
            dict.fromkeys(str(item).strip() for item in raw if str(item).strip())
        )
        if cleaned:
            return cleaned
    return fallback


@dataclass(slots=True, frozen=True)
class UACaller:
    """Pemilih User-Agent dan header bahasa per engine dari konfigurasi engines.toml."""

    desktop_uas: tuple[str, ...] = _DEFAULT_DESKTOP_UAS
    mobile_uas: tuple[str, ...] = _DEFAULT_MOBILE_UAS
    profiles: dict[str, EngineClientProfile] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.profiles is None:
            object.__setattr__(self, "profiles", {})

    @classmethod
    def from_config(cls, ec: Mapping[str, object] | None) -> UACaller:
        """Bangun instance UACaller langsung dari dictionary cfg.engines (engines.toml)."""
        cfg = ec if isinstance(ec, Mapping) else {}
        ua_sec = cfg.get("user_agents")
        ua_map = ua_sec if isinstance(ua_sec, Mapping) else {}

        desktop = _clean_ua_list(ua_map.get("desktop"), _DEFAULT_DESKTOP_UAS)
        mobile = _clean_ua_list(ua_map.get("mobile"), _DEFAULT_MOBILE_UAS)

        prof_sec = cfg.get("profiles")
        prof_map = prof_sec if isinstance(prof_sec, Mapping) else {}
        parsed_profiles: dict[str, EngineClientProfile] = {}

        for raw_name, raw_val in prof_map.items():
            if not isinstance(raw_val, Mapping):
                continue
            name = str(raw_name).strip()
            if not name:
                continue
            dev_raw = str(raw_val.get("device", "desktop")).strip().lower()
            device = (
                DeviceKind.MOBILE
                if dev_raw == DeviceKind.MOBILE
                else DeviceKind.DESKTOP
            )
            accept_lang = str(raw_val.get("accept_lang", "")).strip()
            parsed_profiles[name] = EngineClientProfile(
                device=device,
                accept_lang=accept_lang,
            )

        return cls(
            desktop_uas=desktop,
            mobile_uas=mobile,
            profiles=parsed_profiles,
        )

    def profile_for(self, engine_name: str) -> EngineClientProfile:
        """Ambil profil klien untuk engine tertentu (default: desktop)."""
        if not engine_name:
            return EngineClientProfile()
        base_name = engine_name.split("@", 1)[0]
        return (
            self.profiles.get(base_name)
            or self.profiles.get(engine_name)
            or EngineClientProfile()
        )

    def pick_desktop_ua(self) -> str:
        """Pilih satu User-Agent desktop secara acak."""
        return random.choice(self.desktop_uas)

    def pick_mobile_ua(self) -> str:
        """Pilih satu User-Agent mobile secara acak."""
        return random.choice(self.mobile_uas)

    def pick_ua(self, engine_name: str = "") -> str:
        """Pilih User-Agent sesuai profil device milik engine (desktop atau mobile)."""
        if self.profile_for(engine_name).device is DeviceKind.MOBILE:
            return self.pick_mobile_ua()
        return self.pick_desktop_ua()

    def build_headers(
        self,
        engine_name: str = "",
        *,
        extra: Mapping[str, str] | None = None,
    ) -> HeaderMap:
        """Susun dictionary header HTTP lengkap dengan User-Agent & Accept-Language engine."""
        prof = self.profile_for(engine_name)
        headers: HeaderMap = {"User-Agent": self.pick_ua(engine_name)}
        if prof.accept_lang:
            headers["Accept-Language"] = prof.accept_lang
        if extra:
            headers.update(extra)
        return headers
