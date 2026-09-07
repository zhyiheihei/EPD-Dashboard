"""局部刷新（COMMIT PARTIAL 标志）协议与推送策略测试。"""
import struct
import sys
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


def make_pusher(full_every: int) -> Pusher:
    pusher = object.__new__(Pusher)
    pusher._push_count = 0
    pusher._cfg = type("Cfg", (), {"full_refresh_every": full_every, "commit_sleep": True})()
    return pusher


def test_build_commit_partial_flag():
    assert protocol.build_commit(7, COMMIT_DEFAULT | COMMIT_PARTIAL) == bytes(
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