"""Environment-backed configuration for Huntbox."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    producthunt_token: str | None
    serper_api_key: str | None
    ahrefs_api_key: str | None
    apify_api_token: str | None
    serper_concurrency: int
    serper_delay_seconds: float
    apify_concurrency: int
    domain_age_concurrency: int
    log_level: str
    db_path: Path

    @property
    def has_producthunt(self) -> bool:
        return bool(self.producthunt_token)

    @property
    def has_serper(self) -> bool:
        return bool(self.serper_api_key)

    @property
    def has_ahrefs(self) -> bool:
        return bool(self.ahrefs_api_key)

    @property
    def has_apify(self) -> bool:
        return bool(self.apify_api_token)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def get_settings() -> Settings:
    return Settings(
        producthunt_token=(os.getenv("PRODUCTHUNT_API_TOKEN") or "").strip() or None,
        serper_api_key=(os.getenv("SERPER_API_KEY") or "").strip() or None,
        ahrefs_api_key=(os.getenv("AHREFS_API_KEY") or "").strip() or None,
        apify_api_token=(os.getenv("APIFY_API_TOKEN") or "").strip() or None,
        serper_concurrency=max(1, _int_env("SERPER_CONCURRENCY", 3)),
        serper_delay_seconds=max(0.0, _float_env("SERPER_DELAY_SECONDS", 0.35)),
        apify_concurrency=max(1, _int_env("APIFY_CONCURRENCY", 2)),
        domain_age_concurrency=max(1, _int_env("DOMAIN_AGE_CONCURRENCY", 2)),
        log_level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
        db_path=Path(os.getenv("HUNTBOX_DB") or (BASE_DIR / "data" / "huntbox.db")),
    )


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
