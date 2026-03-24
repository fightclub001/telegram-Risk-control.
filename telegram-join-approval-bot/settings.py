from __future__ import annotations

import logging
import os
from dataclasses import dataclass


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


@dataclass(slots=True, frozen=True)
class Settings:
    """Runtime settings loaded from environment variables."""

    bot_token: str
    webhook_url: str
    port: int = 8080
    log_level: str = "INFO"
    ocr_enabled: bool = True
    ocr_cache_ttl_seconds: int = 24 * 3600
    extra_terms: str = ""
    decline_and_ban: bool = False
    opencc_config: str = "t2s"
    ocr_max_side: int = 512
    request_timeout_seconds: float = 10.0
    webhook_path: str = "/telegram/webhook"
    extra_terms_file: str = "extra_terms.txt"

    @property
    def webhook_endpoint(self) -> str:
        base = self.webhook_url.strip().rstrip("/")
        path = self.webhook_path if self.webhook_path.startswith("/") else f"/{self.webhook_path}"
        return f"{base}{path}"


def load_settings() -> Settings:
    """Load validated settings from environment variables."""
    bot_token = (os.getenv("BOT_TOKEN") or "").strip()
    if not bot_token:
        raise ValueError("BOT_TOKEN is required")

    webhook_url = (os.getenv("WEBHOOK_URL") or "").strip()
    if not webhook_url:
        raise ValueError("WEBHOOK_URL is required")

    return Settings(
        bot_token=bot_token,
        webhook_url=webhook_url,
        port=_as_int(os.getenv("PORT"), 8080),
        log_level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper(),
        ocr_enabled=_as_bool(os.getenv("OCR_ENABLED"), True),
        ocr_cache_ttl_seconds=_as_int(os.getenv("OCR_CACHE_TTL_SECONDS"), 24 * 3600),
        extra_terms=(os.getenv("EXTRA_TERMS") or "").strip(),
        decline_and_ban=_as_bool(os.getenv("DECLINE_AND_BAN"), False),
        opencc_config=(os.getenv("OPENCC_CONFIG") or "t2s").strip(),
        ocr_max_side=_as_int(os.getenv("OCR_MAX_SIDE"), 512),
    )


def configure_logging(level: str) -> None:
    """Initialize concise process-wide logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
