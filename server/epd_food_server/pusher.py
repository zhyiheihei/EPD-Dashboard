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
    stage: str | None = None  # 失败发生的阶段（begin/bitmap/commit/prepare 等）
    device: str | None = None
    items: list[str] = field(default_factory=list)
    device_status: dict | None = None  # 设备诊断状态块（boot 计数/复位原因等，v0x28+）
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
        self._pending_change = False
        self._last_device_status: dict | None = None  # 最近一次会话读到的设备诊断状态

    @property
    def in_progress(self) -> bool:
        return self._in_progress

    # ---------- 状态 ----------

    @property
    def _last_full_refresh_path(self) -> Path:
        return self._cfg.state_dir / "last-full-refresh.txt"

    def _daily_full_refresh_pending(self) -> bool:
        """今天的全刷尚未完成则 True（只读，不落盘）。

        更新逻辑（2026-10 重定义）：固件 v24 起不自动全刷，日期/倒计时更新、
        日程上屏、残影清理全部由服务端负责。每天 0 点的定时推送是当天首次
        推送，自然承担全刷；若 0 点推送失败或错过（服务停机），当天后续
        第一次成功推送补做全刷，保证日期不陈旧。落盘在推送成功之后进行。
        """
        today = datetime.now(timezone.utc).astimezone().date().isoformat()
        try:
            last = self._last_full_refresh_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return True
        except Exception as exc:
            log.warning("上次全刷日期读取失败: %s", exc)
            return True
        return last != today

    def _mark_daily_full_refresh_done(self) -> None:
        try:
            self._cfg.state_dir.mkdir(parents=True, exist_ok=True)
            today = datetime.now(timezone.utc).astimezone().date().isoformat()
            self._last_full_refresh_path.write_text(today, encoding="utf-8")
        except Exception as exc:
            log.warning("上次全刷日期写入失败: %s", exc)

    def _commit_flags(self, caps: protocol.CapsInfo, force_full: bool) -> int:
        """全刷与否由调用方在发起推送前决定（每日首推/日程变化），此处只翻标志。"""
        flags = protocol.COMMIT_DEFAULT
        if not self._cfg.commit_sleep:
            flags &= ~protocol.COMMIT_SLEEP_AFTER
        if not (caps.features & protocol.FEATURE_PARTIAL_REFRESH) or force_full:
            return flags
        return flags | protocol.COMMIT_PARTIAL

    # ---------- 传输失败保护（暂停自动推送） ----------

    @property
    def _paused_path(self) -> Path:
        return self._cfg.state_dir / "push-paused.json"

    @property
    def _failure_path(self) -> Path:
        return self._cfg.state_dir / "last-failure.json"

    def _read_paused(self) -> dict | None:
        try:
            return json.loads(self._paused_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:
            log.warning("推送暂停状态读取失败: %s", exc)
            return None

    def _pause_automated(self, status: PushStatus) -> None:
        """传输失败后暂停自动推送，人工确认前不再碰面板。

        本机全局 journal 极易被冲掉、epd 命名空间也可能轮转：把失败详情
        落盘 state_dir（last-failure.json + push-paused.json），并停止后续
        自动推送，避免错误被下一次推送的日志冲掉。手动推送成功后恢复。
        """
        payload = {
            "at": _iso_now(),
            "reason": status.reason,
            "error": status.error,
            "stage": status.stage,
            "device": status.device,
        }
        try:
            self._cfg.state_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self._paused_path, payload)
            _atomic_write_json(self._failure_path, status.to_dict())
        except Exception as exc:
            log.warning("推送暂停状态写入失败: %s", exc)
        log.error(
            "传输失败，已暂停自动推送（stage=%s error=%s）。恢复方式：WebUI/"  
            "API 手动推送成功，或 epd-food-server push-now --manual",
            status.stage, status.error,
        )

    def _resume(self) -> None:
        try:
            self._paused_path.unlink(missing_ok=True)
            self._failure_path.unlink(missing_ok=True)
        except Exception as exc:
            log.warning("推送暂停状态清理失败: %s", exc)

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
        status["paused"] = self._read_paused()
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

    async def push(self, reason: str, force_full: bool = False) -> PushStatus:
        """执行一次完整推送。API 与 0 点 timer 通过文件锁互斥。

        传输失败后自动推送（timer/change/serve）暂停，直到手动推送成功恢复；
        reason=manual 不受暂停限制，且成功时清除暂停状态。

        force_full=True 跳过每日全刷/日程变化判断强制全刷（服务启动时的
        serve 推送用）：面板 RAM 不随 MCU 复位保留，服务重启后首次推送
        必须全刷才能整个上屏。
        """
        if self._busy.locked():
            return PushStatus(ok=None, reason=reason, error="已有推送在进行中")
        if reason != "manual":
            paused = self._read_paused()
            if paused:
                log.warning(
                    "自动推送已暂停（上次传输失败未恢复），跳过本次 %s 推送: %s",
                    reason, paused.get("error"),
                )
                status = PushStatus(
                    ok=False,
                    reason=reason,
                    error=f"自动推送已暂停，需手动推送恢复（上次失败: {paused.get('error')}）",
                    stage=paused.get("stage"),
                )
                self._write_status(status)
                return status
        async with self._busy:
            status = await asyncio.to_thread(self._push_blocking, reason, force_full)
        if status.ok:
            self._resume()
        elif status.ok is False and status.stage not in (None, "prepare", "lock"):
            # 只有传输链路（扫描/连接/协议事务）失败才暂停；数据库/渲染等
            # 准备阶段错误不代表设备不可达，不阻断后续自动推送
            self._pause_automated(status)
        return status

    def _push_blocking(self, reason: str, force_full: bool = False) -> PushStatus:
        self._in_progress = True
        started = time.monotonic()
        status = PushStatus(reason=reason, started_at=_iso_now())
        lock_fd = self._acquire_lock()
        if lock_fd is None:
            status.error = f"等待推送锁超时（{LOCK_WAIT_TIMEOUT:.0f}s）"
            status.stage = "lock"
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
            # 全刷决策在尝试前定死：失败重试沿用同一决策，成功后才落盘指纹，
            # 否则失败过的日程变化/每日全刷会被静默跳过
            schedule_changed = self._schedules_fingerprint_changed(schedule_bitmaps)
            need_full = force_full or self._daily_full_refresh_pending() or schedule_changed
            if force_full:
                log.info("全刷由调用方强制（%s 推送），本次上屏完整内容", reason)
            elif schedule_changed:
                log.info("日程内容变化，本次全刷上屏")
            elif need_full:
                log.info("今日全刷尚未完成，本次全刷更新日期/倒计时与日程")
            attempts = 1 + max(0, self._cfg.push_retries)
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    status.device, status.device_status = self._run_session(
                        records, bitmaps, schedules, schedule_bitmaps, need_full
                    )
                    status.ok = True
                    last_error = None
                    break
                except (DeviceError, ProtocolError) as exc:
                    last_error = exc
                    status.device_status = self._last_device_status
                    log.warning("第 %s/%s 次推送失败: %s", attempt, attempts, exc)
                    if attempt < attempts:
                        time.sleep(self._cfg.retry_backoff * attempt)
            if last_error is not None:
                status.ok = False
                status.error = str(last_error)
                status.stage = getattr(last_error, "stage", None)
                log.error(
                    "推送最终失败（共 %s 次尝试，device=%s stage=%s）: %s",
                    attempts, status.device, status.stage, status.error,
                )
            else:
                if need_full:
                    self._mark_daily_full_refresh_done()
                self._save_schedules_fingerprint(schedule_bitmaps)
        except Exception as exc:  # DB/渲染等非 BLE 错误
            log.exception("推送准备阶段失败")
            status.ok = False
            status.error = str(exc)
            status.stage = "prepare"
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

    def _schedules_fingerprint_path(self) -> Path:
        return self._cfg.state_dir / "last-schedules.txt"

    def _schedules_fingerprint(self, bitmaps: list[bytes]) -> str:
        return hashlib.sha256(b"".join(bitmaps)).hexdigest()[:16]

    def _schedules_fingerprint_changed(self, bitmaps: list[bytes]) -> bool:
        """日程位图指纹与上次不同返回 True（日程变化需要全刷才能上屏）。

        只读不落盘：指纹在推送成功后才由 _save_schedules_fingerprint 写入，
        推送失败时下次推送仍能检测到变化并重试全刷。
        """
        fingerprint = self._schedules_fingerprint(bitmaps)
        try:
            last = self._schedules_fingerprint_path().read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return True
        except Exception as exc:
            log.warning("日程指纹读取失败: %s", exc)
            return True
        return last != fingerprint

    def _save_schedules_fingerprint(self, bitmaps: list[bytes]) -> None:
        try:
            self._cfg.state_dir.mkdir(parents=True, exist_ok=True)
            self._schedules_fingerprint_path().write_text(
                self._schedules_fingerprint(bitmaps), encoding="utf-8"
            )
        except Exception as exc:
            log.warning("日程指纹写入失败: %s", exc)

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

        async def run() -> tuple[str, dict | None]:
            async with EpdSession(self._cfg) as session:
                caps = await session.handshake()
                # MTU 协商后再读诊断状态块（v28 设备 16B 应答帧在默认 23B
                # MTU 下会被截断）
                self._last_device_status = await session.read_device_status()
                if caps.max_foods < len(foods):
                    log.warning("设备食品上限 %s 少于待发送 %s 条", caps.max_foods, len(foods))
                flags = self._commit_flags(caps, force_full_refresh)
                log.info(
                    "本次推送：%s（device=%s）",
                    "全刷" if not (flags & protocol.COMMIT_PARTIAL) else "局部刷新",
                    session.device_name,
                )
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
                self._settle(3.0 if flags & protocol.COMMIT_PARTIAL else None)
                return session.device_name or "unknown", session.device_status

        return asyncio.run(run())
