"""Config.from_env 环境变量解析（防止默认值漏传这类线上事故）"""

import sys

sys.path.insert(0, "server")

import pytest

from epd_food_server.config import Config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("EPD_FOOD_"):
            monkeypatch.delenv(key, raising=False)


def test_from_env_defaults(monkeypatch):
    cfg = Config.from_env()
    assert cfg.schedule_horizon_days == 365
    assert cfg.change_min_interval == 1800.0
    assert cfg.full_refresh_every == 8


def test_from_env_legacy_schedule_days(monkeypatch):
    monkeypatch.setenv("EPD_FOOD_SCHEDULE_DAYS", "7")
    assert Config.from_env().schedule_horizon_days == 7


def test_from_env_new_schedule_horizon(monkeypatch):
    monkeypatch.setenv("EPD_FOOD_SCHEDULE_HORIZON_DAYS", "180")
    assert Config.from_env().schedule_horizon_days == 180
    # 新变量优先于旧变量
    monkeypatch.setenv("EPD_FOOD_SCHEDULE_DAYS", "7")
    assert Config.from_env().schedule_horizon_days == 180
