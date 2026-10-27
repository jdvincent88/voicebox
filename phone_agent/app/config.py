"""Settings for the phone agent.

Secrets and deployment values come from environment variables. Everything a
manager might want to edit (hours, FAQs, transfer numbers) lives in
business.yaml so it can change without touching code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    public_base_url: str
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_sms_from: str
    anthropic_model: str
    anthropic_effort: str
    square_access_token: str
    square_location_id: str
    square_base_url: str
    parlor_base_url: str
    parlor_api_key: str
    voicebox_url: str
    voicebox_profile: str
    tts_provider: str
    tts_voice: str
    validate_twilio_signature: bool
    business_file: Path
    data_dir: Path

    @property
    def wss_base_url(self) -> str:
        return "wss://" + self.public_base_url.split("://", 1)[-1]


@lru_cache
def get_settings() -> Settings:
    square_env = _env("SQUARE_ENVIRONMENT", "production")
    return Settings(
        public_base_url=_env("PUBLIC_BASE_URL").rstrip("/"),
        twilio_account_sid=_env("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=_env("TWILIO_AUTH_TOKEN"),
        twilio_sms_from=_env("TWILIO_SMS_FROM"),
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-opus-5-5"),
        # Phone callers hear every millisecond of thinking; low effort keeps
        # turns snappy. Raise to "medium" if ordering accuracy needs it.
        anthropic_effort=_env("ANTHROPIC_EFFORT", "low"),
        square_access_token=_env("SQUARE_ACCESS_TOKEN"),
        square_location_id=_env("SQUARE_LOCATION_ID"),
        square_base_url=(
            "https://connect.squareupsandbox.com"
            if square_env == "sandbox"
            else "https://connect.squareup.com"
        ),
        parlor_base_url=_env("PARLOR_BASE_URL", "https://parlorhq.app").rstrip("/"),
        # Optional: in some deployments a proxy injects the key instead.
        parlor_api_key=_env("PARLOR_API_KEY"),
        voicebox_url=_env("VOICEBOX_URL").rstrip("/"),
        voicebox_profile=_env("VOICEBOX_PROFILE"),
        tts_provider=_env("TTS_PROVIDER", "ElevenLabs"),
        tts_voice=_env("TTS_VOICE"),
        validate_twilio_signature=_env("VALIDATE_TWILIO_SIGNATURE", "true").lower() != "false",
        business_file=Path(_env("BUSINESS_FILE", str(ROOT / "business.yaml"))),
        data_dir=Path(_env("DATA_DIR", str(ROOT / "data"))),
    )


@dataclass
class Business:
    name: str
    timezone: str
    info: str
    transfers: dict[str, dict] = field(default_factory=dict)
    staff_sms: list[str] = field(default_factory=list)
    takeout: dict = field(default_factory=dict)
    reservations: dict = field(default_factory=dict)
    greeting: str = ""

    @classmethod
    def load(cls, path: Path) -> "Business":
        if not path.exists():
            path = ROOT / "business.example.yaml"
        raw = yaml.safe_load(path.read_text()) or {}
        return cls(
            name=raw["name"],
            timezone=raw.get("timezone", "America/New_York"),
            info=raw.get("info", ""),
            transfers=raw.get("transfers") or {},
            staff_sms=raw.get("staff_sms") or [],
            takeout=raw.get("takeout") or {},
            reservations=raw.get("reservations") or {},
            greeting=raw.get("greeting")
            or f"Thanks for calling {raw['name']}! How can I help you today?",
        )


@lru_cache
def get_business() -> Business:
    return Business.load(get_settings().business_file)
