"""Configuration from environment variables.

Required: STOCK_API_URL, STOCK_API_KEY, RESEND_API_KEY, RESEND_BASE_URL,
WEBHOOK_SECRET (and PORT, read by the server command). Everything else is
optional and has a sensible default, so the container starts with only the
variables listed in the brief.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

DEFAULT_EMAIL_FROM = "Back in Stock <alerts@backinstock.example>"

REQUIRED_VARS = (
    "STOCK_API_URL",
    "STOCK_API_KEY",
    "RESEND_API_KEY",
    "RESEND_BASE_URL",
    "WEBHOOK_SECRET",
)


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    stock_api_url: str
    stock_api_key: str
    resend_api_key: str
    resend_base_url: str
    webhook_secret: str
    admin_password: str | None = None
    data_dir: Path = field(default_factory=lambda: Path("data"))
    email_from: str = DEFAULT_EMAIL_FROM
    store_name: str = "Back in Stock"
    store_url: str | None = None
    reconcile_interval: float = 60.0
    reconcile_initial_delay: float = 5.0
    worker_poll_interval: float = 1.0
    stock_timeout: float = 5.0
    email_timeout: float = 10.0
    max_body_bytes: int = 16 * 1024
    run_background: bool = True
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def effective_admin_password(self) -> str:
        """ADMIN_PASSWORD if set, otherwise WEBHOOK_SECRET (so no extra var is required)."""
        return self.admin_password or self.webhook_secret

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env

        def get(name: str) -> str | None:
            value = env.get(name)
            if value is None:
                return None
            value = value.strip()
            return value or None

        missing = [name for name in REQUIRED_VARS if not get(name)]
        if missing:
            raise ConfigError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )

        def url(name: str) -> str:
            value = get(name) or ""
            if not value.startswith(("http://", "https://")):
                raise ConfigError(f"{name} must be an http(s) URL")
            return value.rstrip("/")

        def positive_float(name: str, default: float) -> float:
            raw = get(name)
            if raw is None:
                return default
            try:
                value = float(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be a number") from exc
            if value <= 0:
                raise ConfigError(f"{name} must be greater than zero")
            return value

        store_url = get("STORE_URL")
        if store_url and not store_url.startswith(("http://", "https://")):
            raise ConfigError("STORE_URL must be an http(s) URL")

        return cls(
            stock_api_url=url("STOCK_API_URL"),
            stock_api_key=get("STOCK_API_KEY") or "",
            resend_api_key=get("RESEND_API_KEY") or "",
            resend_base_url=url("RESEND_BASE_URL"),
            webhook_secret=get("WEBHOOK_SECRET") or "",
            admin_password=get("ADMIN_PASSWORD"),
            data_dir=Path(get("DATA_DIR") or "data"),
            email_from=get("EMAIL_FROM") or DEFAULT_EMAIL_FROM,
            store_name=get("STORE_NAME") or "Back in Stock",
            store_url=store_url,
            reconcile_interval=positive_float("RECONCILE_INTERVAL_SECONDS", 60.0),
            log_level=(get("LOG_LEVEL") or "INFO").upper(),
        )
