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


def make_pusher(full_every: int, daily_full_done: bool = True) -> Pusher:
    pusher = object.__new__(Pusher)
    pusher._push_count = 0
    state_dir = Path(tempfile.mkdtemp(prefix="epd-partial-test-"))
    if daily_full_done:
        today = datetime.now(timezone.utc).astimezone().date().isoformat()
        (state_dir / "last-full-refresh.txt").write_text(today, encoding="utf-8")
    pusher._cfg = type(
        "Cfg",
        (),
        {"full_refresh_every": full_every, "commit_sleep": True, "state_dir": state_dir},
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
    pusher = make_pusher(8)
    caps = make_caps(0x003F)  # 无局部刷新特征位
    for _ in range(20):
        assert pusher._commit_flags(caps) == COMMIT_DEFAULT


def test_commit_flags_daily_first_push_full_refresh():
    """当天首次推送必须全刷：固件已去掉午夜自动刷新，全刷由服务端负责。"""
    pusher = make_pusher(8, daily_full_done=False)
    caps = make_caps(0x007F)
    assert not pusher._commit_flags(caps) & COMMIT_PARTIAL
    # 同一天后续推送恢复局部
    assert pusher._commit_flags(caps) & COMMIT_PARTIAL


def test_commit_flags_daily_date_persisted_and_reloaded():
    """上次全刷日期持久化：新 Pusher 实例（模拟重启）同日不再全刷。"""
    state_dir = Path(tempfile.mkdtemp(prefix="epd-partial-test-"))
    today = datetime.now(timezone.utc).astimezone().date().isoformat()
    pusher_a = object.__new__(Pusher)
    pusher_a._push_count = 0
    pusher_a._cfg = type(
        "Cfg", (), {"full_refresh_every": 8, "commit_sleep": True, "state_dir": state_dir}
    )()
    caps = make_caps(0x007F)
    assert not pusher_a._commit_flags(caps) & COMMIT_PARTIAL  # 无记录 → 全刷
    pusher_b = object.__new__(Pusher)
    pusher_b._push_count = 0
    pusher_b._cfg = pusher_a._cfg
    assert pusher_b._commit_flags(caps) & COMMIT_PARTIAL  # 已有今日记录 → 局部
    assert (state_dir / "last-full-refresh.txt").read_text() == today


def test_commit_flags_partial_between_full_cycles():
    pusher = make_pusher(3)
    caps = make_caps(0x007F)
    # 第 1、2 次局部，第 3 次全刷，循环
    assert pusher._commit_flags(caps) & COMMIT_PARTIAL
    assert pusher._commit_flags(caps) & COMMIT_PARTIAL
    flags = pusher._commit_flags(caps)
    assert not flags & COMMIT_PARTIAL
    assert flags & COMMIT_REFRESH and flags & COMMIT_SLEEP_AFTER


def test_commit_flags_full_every_zero():
    pusher = make_pusher(0)
    caps = make_caps(0x007F)
    assert pusher._commit_flags(caps) == COMMIT_DEFAULT


def test_commit_flags_partial_every_two():
    pusher = make_pusher(2)
    caps = make_caps(0x007F)
    assert pusher._commit_flags(caps) & COMMIT_PARTIAL
    assert not pusher._commit_flags(caps) & COMMIT_PARTIAL