"""推送编排：Top4 选择、跨进程文件锁、状态落盘、变更防抖。"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import protocol, render
from .ble import DeviceError, EpdSession
from .config import DRINK_CATEGORY, Config
from .db import Database
from .protocol import FoodRecord, ProtocolError, ScheduleRecord

log = logging.getLogger(__name__)

LOCK_WAIT_TIMEOUT = 90.0

# push-history.jsonl 保留的最近推送条数；journal 可能被冲掉，状态文件只存最后一次，
# 历史落盘在 state_dir 才能事后查「哪次失败、为什么失败」
PUSH_HISTORY_KEEP = 100

WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


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
        self._pending_change = False

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
        self._append_history(status)

    def _append_history(self, status: PushStatus) -> None:
        path = self._cfg.state_dir / "push-history.jsonl"
        try:
            self._cfg.state_dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(status.to_dict(), ensure_ascii=False) + "\n")
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > PUSH_HISTORY_KEEP:
                tmp = path.with_suffix(".tmp")
                tmp.write_text("\n".join(lines[-PUSH_HISTORY_KEEP:]) + "\n", encoding="utf-8")
                os.replace(tmp, path)
        except Exception as exc:
            log.warning("推送历史写入失败: %s", exc)

    # ---------- 防抖（serve 模式） ----------

    def request_change_push(self) -> bool:
        """数据变更后调用：防抖合并连续写入 + 节流限制推送频率。

        距上次成功推送不足 change_min_interval 秒则不推（墨水屏刷新寿命有限），
        变更会记为 pending，在 0 点定时推送或下次节流到期后的变更时一并上屏。
        """
        if not self._cfg.push_on_change:
            return False
        if self._recently_pushed():
            self._pending_change = True
            log.info("距上次推送不足 %s 分钟，变更留待下次推送", self._cfg.change_min_interval / 60)
            return False
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounced_push(self._cfg.push_debounce))
        return True

    def _recently_pushed(self) -> bool:
        try:
            mtime = self._cfg.status_path.stat().st_mtime
        except OSError:
            return False
        ok = False
        try:
            ok = json.loads(self._cfg.status_path.read_text(encoding="utf-8")).get("ok") is True
        except Exception:
            pass
        return ok and (time.time() - mtime) < self._cfg.change_min_interval

    async def _debounced_push(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self._busy.locked():
                # 已有推送进行中：数据会在那次推送里带上（DB 已是最新），
                # 或留待下次变更触发，不排队白等锁
                log.info("推送进行中，防抖推送跳过（数据已含最新变更）")
                return
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
            schedules, schedule_bitmaps = self._fetch_schedules()
            schedule_changed = self._schedules_changed(schedule_bitmaps)
            attempts = 1 + max(0, self._cfg.push_retries)
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    status.device = self._run_session(
                        records, bitmaps, schedules, schedule_bitmaps, schedule_changed
                    )
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

    # ---------- 日程栏（CalDAV 只读） ----------

    @property
    def _schedule_cache_path(self) -> Path:
        return self._cfg.state_dir / "schedules-cache.json"

    def _save_schedule_cache(
        self, schedules: list[ScheduleRecord], bitmaps: list[bytes]
    ) -> None:
        """成功拉取后落盘，供下次拉取失败时沿用（避免食品变更推送擦掉日程栏）。"""
        payload = [
            {"start_utc": s.start_utc, "bitmap": bitmap.hex()}
            for s, bitmap in zip(schedules, bitmaps)
        ]
        try:
            self._cfg.state_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self._schedule_cache_path, {"schedules": payload})
        except Exception as exc:
            log.warning("日程缓存写入失败: %s", exc)

    def _load_schedule_cache(self) -> tuple[list[ScheduleRecord], list[bytes]]:
        try:
            data = json.loads(self._schedule_cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], []
        except Exception as exc:
            log.warning("日程缓存读取失败: %s", exc)
            return [], []
        schedules: list[ScheduleRecord] = []
        bitmaps: list[bytes] = []
        try:
            for index, item in enumerate(data.get("schedules", [])):
                schedules.append(
                    ScheduleRecord(slot=index, start_utc=int(item["start_utc"]))
                )
                bitmaps.append(bytes.fromhex(item["bitmap"]))
        except Exception as exc:
            log.warning("日程缓存格式异常，忽略: %s", exc)
            return [], []
        return schedules, bitmaps

    def _fetch_schedules(
        self,
    ) -> tuple[list[protocol.ScheduleRecord], list[bytes]]:
        """拉取 CalDAV 日程并渲染标题位图（槽 0x00/0x01，320×20）。

        展示策略是「最近 N 条」而非「最近 N 天内」：拉取窗口放大到
        schedule_horizon_days 兜底，按开始时间排序取最近的几条，
        数月后的长期日程同样能上屏。
        拉取或解析失败降级沿用上次成功的日程（state_dir 缓存），
        没有缓存才退化为无日程——避免食品变更推送恰好赶上网络抖动
        时把屏幕上的日程栏擦掉。
        未配置 CalDAV 也返回空。
        """
        if not self._cfg.caldav_enabled:
            return [], []
        from .caldav import CalDavClient, CalDavConfig

        zone = ZoneInfo(self._cfg.timezone)
        now = datetime.now(timezone.utc)
        window_end = now + timedelta(days=self._cfg.schedule_horizon_days)
        try:
            client = CalDavClient(
                CalDavConfig(
                    url=self._cfg.caldav_url,
                    user=self._cfg.caldav_user,
                    password=self._cfg.caldav_password,
                    calendar=self._cfg.caldav_calendar,
                )
            )
            events = client.fetch_events(now, window_end, zone)
            upcoming = [e for e in events if e.start_utc >= now]
            upcoming.sort(key=lambda e: e.start_utc)
        except Exception as exc:
            cached = self._load_schedule_cache()
            if cached:
                log.warning("CalDAV 日程拉取失败，沿用上次日程: %s", exc)
                return cached
            log.warning("CalDAV 日程拉取失败，本次推送无日程: %s", exc)
            return [], []

        schedules: list[protocol.ScheduleRecord] = []
        bitmaps: list[bytes] = []
        for index, event in enumerate(upcoming[: protocol.MAX_SCHEDULES]):
            schedules.append(
                protocol.ScheduleRecord(
                    slot=index, start_utc=int(event.start_utc.timestamp())
                )
            )
            # 标题带上开始时间（如 “周五 周会”），纯标题无法区分远近日程
            local_start = event.start_utc.astimezone(zone)
            label = f"{WEEKDAY_CN[local_start.weekday()]} {event.summary}"
            bitmaps.append(
                render.render_text_1bit(
                    label,
                    protocol.SCHEDULE_BITMAP_WIDTH,
                    protocol.SCHEDULE_BITMAP_HEIGHT,
                    self._font,
                )
            )
        self._save_schedule_cache(schedules, bitmaps)
        return schedules, bitmaps

    def _schedules_changed(self, bitmaps: list[bytes]) -> bool:
        """日程位图指纹与上次不同返回 True（日程变化需要全刷才能上屏）。"""
        fingerprint = hashlib.sha256(b"".join(bitmaps)).hexdigest()[:16]
        path = self._cfg.state_dir / "last-schedules.txt"
        try:
            last = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            last = ""
        except Exception as exc:
            log.warning("日程指纹读取失败: %s", exc)
            last = ""
        try:
            self._cfg.state_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(fingerprint, encoding="utf-8")
        except Exception as exc:
            log.warning("日程指纹写入失败: %s", exc)
        return last != fingerprint

    def _run_session(
        self,
        foods: list[FoodRecord],
        bitmaps: list[bytes],
        schedules: list[ScheduleRecord],
        schedule_bitmaps: list[bytes],
        force_full_refresh: bool,
    ) -> str:
        zone = ZoneInfo(self._cfg.timezone)
        now_utc = int(time.time())
        tz_minutes = int(datetime.now(zone).utcoffset().total_seconds() // 60)

        async def run() -> str:
            async with EpdSession(self._cfg) as session:
                caps = await session.handshake()
                if caps.max_foods < len(foods):
                    log.warning("设备食品上限 %s 少于待发送 %s 条", caps.max_foods, len(foods))
                flags = self._commit_flags(caps)
                if force_full_refresh and (flags & protocol.COMMIT_PARTIAL):
                    log.info("日程变化，本次强制全刷")
                    flags &= ~protocol.COMMIT_PARTIAL
                partial = bool(flags & protocol.COMMIT_PARTIAL)
                await session.push_foods(
                    foods[: caps.max_foods],
                    bitmaps[: caps.max_foods],
                    now_utc,
                    tz_minutes,
                    schedules=schedules[: min(protocol.MAX_SCHEDULES, caps.max_schedules)],
                    schedule_bitmaps=schedule_bitmaps,
                    commit_flags=flags,
                )
                # 局部刷新仅刷新 ~1-2s，无需等全刷的 16s
                self._settle(3.0 if partial else None)
                return session.device_name or "unknown"

        return asyncio.run(run())
