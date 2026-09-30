"""局部刷新（COMMIT PARTIAL 标志）协议与推送策略测试。"""
import struct
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from epd_food_server import protocol
from epd_food_server.protocol import (
    Response,
    parse_caps_response,
    build_commit,
    FEATURE_PARTIAL_REFRESH,
    COMMIT_REFRESH,
    COMMIT_SLEEP_AFTER,
    COMMIT_PARTIAL,
    COMMIT_DEFAULT,
)
from epd_food_server.pusher import Pusher


def make_caps(features: int) -> protocol.CapsInfo:
    payload = bytes([0x21, 0x01, 0x02, 0x04]) + struct.pack(
        ">HHHHH", 1024, features, 244, 800, 480
    )
    return parse_caps_response(
        Response(protocol=1, transaction=0, command=0x40, status=0, payload=payload)
    )


def make_pusher(daily_full_done: bool = True) -> Pusher:
    pusher = object.__new__(Pusher)
    state_dir = Path(tempfile.mkdtemp(prefix="epd-partial-test-"))
    if daily_full_done:
        today = datetime.now(timezone.utc).astimezone().date().isoformat()
        (state_dir / "last-full-refresh.txt").write_text(today, encoding="utf-8")
    pusher._cfg = type(
        "Cfg", (), {"commit_sleep": True, "state_dir": state_dir}
    )()
    return pusher


def test_build_commit_partial_flag():
    assert build_commit(7, COMMIT_DEFAULT | COMMIT_PARTIAL) == bytes(
        [0x43, 0x01, 0x07, 0x07]
    )


def test_caps_feature_bit6():
    assert make_caps(0x003F).features & FEATURE_PARTIAL_REFRESH == 0
    assert make_caps(0x007F).features & FEATURE_PARTIAL_REFRESH


def test_commit_flags_unsupported_device_always_full():
    pusher = make_pusher()
    caps = make_caps(0x003F)  # 无局部刷新特征位
    assert pusher._commit_flags(caps, force_full=False) == COMMIT_DEFAULT
    assert pusher._commit_flags(caps, force_full=True) == COMMIT_DEFAULT


def test_commit_flags_forced_full_refresh():
    """每日首推/日程变化 → force_full=True → 全刷；其余局部。"""
    pusher = make_pusher()
    caps = make_caps(0x007F)
    assert pusher._commit_flags(caps, force_full=True) == COMMIT_DEFAULT
    flags = pusher._commit_flags(caps, force_full=False)
    assert flags & COMMIT_PARTIAL
    assert flags & COMMIT_REFRESH and flags & COMMIT_SLEEP_AFTER


def test_commit_flags_commit_sleep_disabled():
    pusher = make_pusher()
    pusher._cfg.commit_sleep = False
    caps = make_caps(0x007F)
    flags = pusher._commit_flags(caps, force_full=False)
    assert flags & COMMIT_PARTIAL and not flags & COMMIT_SLEEP_AFTER
    assert pusher._commit_flags(caps, force_full=True) == COMMIT_REFRESH


def test_daily_full_refresh_pending_and_persisted():
    """全刷决策只读；标记在推送成功后落盘，重启同日不再全刷。"""
    pusher = make_pusher(daily_full_done=False)
    state_dir = pusher._cfg.state_dir
    assert pusher._daily_full_refresh_pending() is True
    assert not (state_dir / "last-full-refresh.txt").exists()  # 决策只读不落盘
    pusher._mark_daily_full_refresh_done()
    assert pusher._daily_full_refresh_pending() is False
    today = datetime.now(timezone.utc).astimezone().date().isoformat()
    assert (state_dir / "last-full-refresh.txt").read_text() == today
    # 新实例（模拟重启）同日不再全刷
    pusher_b = object.__new__(Pusher)
    pusher_b._cfg = pusher._cfg
    assert pusher_b._daily_full_refresh_pending() is False

# ---------- 变更推送节流（CHANGE_MIN_INTERVAL） ----------

def make_throttle_pusher(min_interval, ok=None, pushed_just_now=False):
    import json as _json
    import os as _os
    import tempfile as _tempfile
    import time as _time
    pusher = object.__new__(Pusher)
    pusher._debounce_task = None
    pusher._pending_change = False
    state = Path(_tempfile.mkdtemp())
    pusher._cfg = type("Cfg", (), {
        "push_on_change": True, "push_debounce": 0,
        "change_min_interval": min_interval, "state_dir": state,
        "status_path": state / "push-status.json",
    })()
    if ok is not None:
        pusher._cfg.status_path.write_text(_json.dumps({"ok": ok}))
        if pushed_just_now:
            _os.utime(pusher._cfg.status_path, (_time.time(), _time.time()))
    return pusher


def test_throttle_first_change_schedules():
    import asyncio
    pusher = make_throttle_pusher(1800)
    assert asyncio.run(_sched(pusher)) is True


async def _sched(pusher):
    return pusher.request_change_push()


def test_throttle_recent_success_suppressed():
    import asyncio
    pusher = make_throttle_pusher(1800, ok=True, pushed_just_now=True)
    assert asyncio.run(_sched(pusher)) is False
    assert pusher._pending_change is True


def test_throttle_failed_push_not_throttled():
    import asyncio
    pusher = make_throttle_pusher(1800, ok=False, pushed_just_now=True)
    assert asyncio.run(_sched(pusher)) is True


def test_throttle_zero_interval_always_schedules():
    import asyncio
    pusher = make_throttle_pusher(0, ok=True, pushed_just_now=True)
    assert asyncio.run(_sched(pusher)) is True
