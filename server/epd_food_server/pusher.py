"""推送编排：Top4 选择、跨进程文件锁、状态落盘、变更防抖。"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import protocol, render
from .ble import DeviceError, EpdSession
from .config import DRINK_CATEGORY, Config
from .db import Database
from .protocol import FoodRecord, ProtocolError

log = logging.getLogger(__name__)

LOCK_WAIT_TIMEOUT = 90.0


@dataclass
class PushStatus:
    ok: bool | None = None
    reason: str = ""
    error: str | None = None
    device: str | None = None
    items: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Pusher:
    def __init__(self, cfg: Config, db: Database):
        self._cfg = cfg
        self._db = db
        self._font = render.find_font(cfg.font_path)
        self._busy = asyncio.Lock()
        self._debounce_task: asyncio.Task | None = None
        self._in_progress = False
        self._push_count = 0

    @property
    def in_progress(self) -> bool:
        return self._in_progress

    # ---------- 状态 ----------

    @property
    def _last_full_refresh_path(self) -> Path:
        return self._cfg.state_dir / "last-full-refresh.txt"

    def _consume_daily_full_refresh(self) -> bool:
        """当天首次推送返回 True（需要全刷）。

        固件 v24 起不再午夜自动全刷（会把服务端位图盖掉），日期/倒计时更新、
        残影清理都依赖每日一次的服务端全刷推送。上次全刷日期持久化在
        state_dir，服务端重启不重置。
        """
        today = datetime.now(timezone.utc).astimezone().date().isoformat()
        try:
            last = self._last_full_refresh_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            last = ""
        except Exception as exc:
            log.warning("上次全刷日期读取失败: %s", exc)
            last = ""
        if last == today:
            return False
        try:
            self._cfg.state_dir.mkdir(parents=True, exist_ok=True)
            self._last_full_refresh_path.write_text(today, encoding="utf-8")
        except Exception as exc:
            log.warning("上次全刷日期写入失败: %s", exc)
        return True

    def _commit_flags(self, caps: protocol.CapsInfo) -> int:
        """决定本次 COMMIT 是否局部刷新：当天首次或达到周期时全刷，否则局部。

        局部刷新不闪屏、耗时短，但残影会累积；每天首次推送强制全刷，
        负责日期/倒计时全屏更新与残影清理（固件不再午夜自动刷新）。
        """
        every = self._cfg.full_refresh_every
        flags = protocol.COMMIT_DEFAULT
        if not self._cfg.commit_sleep:
            flags &= ~protocol.COMMIT_SLEEP_AFTER
        if not (caps.features & protocol.FEATURE_PARTIAL_REFRESH):
            return flags
        self._push_count += 1
        if self._consume_daily_full_refresh():
            log.info("当天首次推送，全刷更新日期与倒计时并清残影")
            return flags
        if every <= 0:
            return flags
        if self._push_count % every == 0:
            log.info("达到全刷周期（第 %s 次），本次全刷清残影", self._push_count)
            return flags
        return flags | protocol.COMMIT_PARTIAL

    def read_status(self) -> dict:
        """合并文件中的最近一次结果与内存中的进行中标记。"""
        status: dict = {}
        try:
            status = json.loads(self._cfg.status_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("推送状态文件读取失败: %s", exc)
        status["in_progress"] = self._in_progress
        return status

    def _write_status(self, status: PushStatus) -> None:
        try:
            self._cfg.status_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self._cfg.status_path, status.to_dict())
        except Exception as exc:
            log.warning("推送状态文件写入失败: %s", exc)

    # ---------- 防抖（serve 模式） ----------

    def request_change_push(self) -> bool:
        """数据变更后调用；防抖合并连续写入。返回是否已调度。"""
        if not self._cfg.push_on_change:
            return False
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounced_push(self._cfg.push_debounce))
        return True

    async def _debounced_push(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            log.info("数据变更防抖到期，自动推送")
            await self.push(reason="change")
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("变更推送失败")

    # ---------- 主流程 ----------

    async def push(self, reason: str) -> PushStatus:
        """执行一次完整推送。API 与 0 点 timer 通过文件锁互斥。"""
        if self._busy.locked():
            return PushStatus(ok=None, reason=reason, error="已有推送在进行中")
        async with self._busy:
            return await asyncio.to_thread(self._push_blocking, reason)

    def _push_blocking(self, reason: str) -> PushStatus:
        self._in_progress = True
        started = time.monotonic()
        status = PushStatus(reason=reason, started_at=_iso_now())
        lock_fd = self._acquire_lock()
        if lock_fd is None:
            status.error = f"等待推送锁超时（{LOCK_WAIT_TIMEOUT:.0f}s）"
            status.finished_at = _iso_now()
            status.duration_s = round(time.monotonic() - started, 1)
            self._write_status(status)
            self._in_progress = False
            return status
        try:
            rows = self._db.top_for_display(limit=protocol.MAX_FOODS)
            status.items = [row["name"] for row in rows]
            records, bitmaps = self._build_entries(rows)
            attempts = 1 + max(0, self._cfg.push_retries)
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    status.device = self._run_session(records, bitmaps)
                    status.ok = True
                    last_error = None
                    break
                except (DeviceError, ProtocolError) as exc:
                    last_error = exc
                    log.warning("第 %s/%s 次推送失败: %s", attempt, attempts, exc)
                    if attempt < attempts:
                        time.sleep(self._cfg.retry_backoff * attempt)
            if last_error is not None:
                status.ok = False
                status.error = str(last_error)
        except Exception as exc:  # DB/渲染等非 BLE 错误
            log.exception("推送准备阶段失败")
            status.ok = False
            status.error = str(exc)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)  # os.open 返回 int fd
            self._settle()
            status.duration_s = round(time.monotonic() - started, 1)
            status.finished_at = _iso_now()
            self._in_progress = False
            self._write_status(status)
        return status

    def _acquire_lock(self):
        """flock 非阻塞重试，直至超时；返回持锁 fd 或 None。"""
        self._cfg.state_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT_TIMEOUT
        while True:
            fd = os.open(self._cfg.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                os.close(fd)
                if time.monotonic() >= deadline:
                    return None
                time.sleep(1.0)

    def _settle(self, seconds: float | None = None) -> None:
        """COMMIT OK 在物理刷新前发出，留出屏幕刷新时间再释放锁。"""
        seconds = self._cfg.settle_seconds if seconds is None else seconds
        if seconds > 0:
            time.sleep(seconds)

    def _build_entries(self, rows: list[dict]) -> tuple[list[FoodRecord], list[bytes]]:
        zone = ZoneInfo(self._cfg.timezone)
        records: list[FoodRecord] = []
        bitmaps: list[bytes] = []
        for index, row in enumerate(rows):
            expiry_local = datetime.combine(row["expiry_date"], dtime(23, 59, 59), tzinfo=zone)
            records.append(
                FoodRecord(
                    slot=index,
                    type=protocol.FOOD_TYPE_DRINK
                    if row["category"] == DRINK_CATEGORY
                    else protocol.FOOD_TYPE_FOOD,
                    expiry_utc=int(expiry_local.timestamp()),
                )
            )
            bitmaps.append(
                render.render_text_1bit(
                    row["name"],
                    protocol.FOOD_BITMAP_WIDTH,
                    protocol.FOOD_BITMAP_HEIGHT,
                    self._font,
                )
            )
        return records, bitmaps

    def _run_session(self, foods: list[FoodRecord], bitmaps: list[bytes]) -> str:
        zone = ZoneInfo(self._cfg.timezone)
        now_utc = int(time.time())
        tz_minutes = int(datetime.now(zone).utcoffset().total_seconds() // 60)

        async def run() -> str:
            async with EpdSession(self._cfg) as session:
                caps = await session.handshake()
                if caps.max_foods < len(foods):
                    log.warning("设备食品上限 %s 少于待发送 %s 条", caps.max_foods, len(foods))
                flags = self._commit_flags(caps)
                partial = bool(flags & protocol.COMMIT_PARTIAL)
                await session.push_foods(
                    foods[: caps.max_foods],
                    bitmaps[: caps.max_foods],
                    now_utc,
                    tz_minutes,
                    commit_flags=flags,
                )
                # 局部刷新仅刷新 ~1-2s，无需等全刷的 16s
                self._settle(3.0 if partial else None)
                return session.device_name or "unknown"

        return asyncio.run(run())
