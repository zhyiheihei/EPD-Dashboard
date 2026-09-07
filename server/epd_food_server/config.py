"""运行配置：全部来自 EPD_FOOD_* 环境变量，与 NixOS 模块（deploy/food-storage.nix）一一对应。"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

# 品类型预设：客户端下拉框与服务端 type 映射共用
PRESET_CATEGORIES = ["食品", "饮品", "生鲜", "乳制品", "速冻", "调味品", "零食", "主食", "药材"]
# 设备协议中 type=1 仅表示"饮品"，其余一律 type=0
DRINK_CATEGORY = "饮品"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(f"EPD_FOOD_{name}")
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def default_state_dir() -> Path:
    """NixOS 上由模块显式指定；开发时退回用户态目录。"""
    if os.geteuid() == 0:
        return Path("/var/lib/epd-dashboard")
    return Path.home() / ".local/state/epd-food-server"


@dataclass(frozen=True)
class Config:
    dsn: str
    api_token: str
    bind_host: str
    bind_port: int
    timezone: str
    font_path: str | None
    # BLE
    device_name_prefix: str
    device_address: str | None
    scan_timeout: float
    connect_timeout: float
    session_timeout: float
    max_chunk: int  # 单片数据字节上限，0 = 按 MTU 自动
    settle_seconds: float  # COMMIT OK 后等待屏幕物理刷新的时间
    push_retries: int
    retry_backoff: float
    # 推送策略
    push_on_change: bool
    push_debounce: float
    full_refresh_every: int  # 每 N 次推送强制全刷清残影；0 = 始终全刷
    commit_sleep: bool  # COMMIT 是否带 SLEEP 标志（调试局部刷新时关闭）
    change_min_interval: float  # 变更推送最小间隔（秒）：墨水屏刷新寿命有限
    state_dir: Path

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "push.lock"

    @property
    def status_path(self) -> Path:
        return self.state_dir / "push-status.json"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            dsn=_env("DSN", "host=/run/postgresql dbname=epd_dashboard"),
            api_token=_env("API_TOKEN", "") or "",
            bind_host=_env("BIND_HOST", "127.0.0.1"),
            bind_port=_env_int("BIND_PORT", 8386),
            timezone=_env("TIMEZONE", "Asia/Shanghai"),
            font_path=_env("FONT_PATH"),
            device_name_prefix=_env("DEVICE_NAME_PREFIX", "NRF_EPD"),
            device_address=_env("DEVICE_ADDRESS"),
            scan_timeout=_env_float("SCAN_TIMEOUT", 15.0),
            connect_timeout=_env_float("CONNECT_TIMEOUT", 10.0),
            session_timeout=_env_float("SESSION_TIMEOUT", 60.0),
            max_chunk=_env_int("MAX_CHUNK", 0),
            settle_seconds=_env_float("SETTLE_SECONDS", 16.0),
            push_retries=_env_int("PUSH_RETRIES", 2),
            retry_backoff=_env_float("RETRY_BACKOFF", 3.0),
            push_on_change=_env_bool("PUSH_ON_CHANGE", True),
            push_debounce=_env_float("PUSH_DEBOUNCE", 10.0),
            change_min_interval=_env_float("CHANGE_MIN_INTERVAL", 1800.0),
            full_refresh_every=_env_int("FULL_REFRESH_EVERY", 8),
            commit_sleep=_env_bool("COMMIT_SLEEP", True),
            state_dir=Path(_env("STATE_DIR") or default_state_dir()),
        )


def ensure_state_dir(cfg: Config) -> Path:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg.state_dir


def tempdir_prefix() -> Path:
    return Path(tempfile.gettempdir())
