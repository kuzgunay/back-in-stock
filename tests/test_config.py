from __future__ import annotations

from pathlib import Path

import pytest

from app.config import ConfigError, Settings

BASE = {
    "STOCK_API_URL": "https://stock.example.com/products/",
    "STOCK_API_KEY": "sk",
    "RESEND_API_KEY": "rk",
    "RESEND_BASE_URL": "https://api.resend.com",
    "WEBHOOK_SECRET": "wh",
}


def test_only_the_documented_variables_are_required():
    s = Settings.from_env(BASE)
    assert s.stock_api_url == "https://stock.example.com/products"
    assert s.effective_admin_password == "wh"  # falls back to WEBHOOK_SECRET
    assert s.db_path == Path("data") / "app.db"
    assert s.reconcile_interval == 60.0


def test_missing_variables_are_listed():
    env = {k: v for k, v in BASE.items() if k not in ("STOCK_API_KEY", "WEBHOOK_SECRET")}
    with pytest.raises(ConfigError, match="STOCK_API_KEY, WEBHOOK_SECRET"):
        Settings.from_env(env)


def test_optional_overrides():
    s = Settings.from_env({**BASE, "ADMIN_PASSWORD": "admin", "DATA_DIR": "/data",
                           "RECONCILE_INTERVAL_SECONDS": "15", "EMAIL_FROM": "Shop <a@b.co>"})
    assert s.effective_admin_password == "admin"
    assert s.db_path == Path("/data") / "app.db"
    assert s.reconcile_interval == 15.0
    assert s.email_from == "Shop <a@b.co>"


@pytest.mark.parametrize("override", [
    {"STOCK_API_URL": "stock.example.com"},
    {"RECONCILE_INTERVAL_SECONDS": "0"},
    {"RECONCILE_INTERVAL_SECONDS": "soon"},
])
def test_bad_values_fail_fast(override):
    with pytest.raises(ConfigError):
        Settings.from_env({**BASE, **override})
