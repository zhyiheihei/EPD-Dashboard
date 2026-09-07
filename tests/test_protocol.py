"""看板协议 v1 帧构造测试：黄金值与结构断言。"""

import struct

import pytest

from epd_food_server import protocol
from epd_food_server.protocol import (
    FoodRecord,
    ProtocolError,
    Response,
    ScheduleRecord,
    build_abort,
    build_begin,
    build_caps_request,
    build_commit,
    build_sync_time,
    crc16_ccitt,
    bitmap_packets,
    parse_caps_response,
    parse_mtu_text,
    parse_response,
    status_name,
)


def test_crc16_ccitt_false_known_vectors():
    assert crc16_ccitt(b"123456789") == 0x29B1
    assert crc16_ccitt(b"") == 0xFFFF
    assert crc16_ccitt(bytes([0xAA, 0x55])) == 0xE5EA
    assert crc16_ccitt(bytes([0x01, 0x02])) == 0x0E7C


def test_build_caps_request_exact_bytes():
    assert build_caps_request() == bytes([0x40, 0x01])


def test_build_begin_hand_computed_golden():
    """手工推导黄金帧：tx=1, now=0, tz=+480, 周一起始, 1 条饮品食品。"""
    frame = build_begin(
        transaction=1,
        now_utc=0,
        timezone_minutes=480,
        week_start=1,
        foods=[FoodRecord(slot=0, type=1, expiry_utc=1)],
    )
    assert frame == bytes(
        [
            0x41, 0x01, 0x01, 0x00,  # cmd, ver, tx, flags
            0x00, 0x00, 0x00, 0x00,  # now_utc BE32
            0x01, 0xE0,  # tz +480 BE16
            0x01,  # week_start
            0x00,  # sched_cnt
            0x01,  # food_cnt
            0x00, 0x01,  # food: slot=0, type=1
            0x00, 0x00, 0x00, 0x01,  # expiry BE32
        ]
    )
    assert len(frame) == 13 + 6


def test_build_begin_with_schedule_length():
    frame = build_begin(
        transaction=7,
        now_utc=1_700_000_000,
        timezone_minutes=480,
        schedules=[ScheduleRecord(slot=0, start_utc=1_700_000_100)],
        foods=[FoodRecord(slot=0, type=0, expiry_utc=1_700_000_200)],
    )
    assert len(frame) == 13 + 10 + 6
    assert frame[0:4] == bytes([0x41, 0x01, 0x07, 0x00])
    assert struct.unpack_from(">I", frame, 4)[0] == 1_700_000_000
    assert frame[11] == 1 and frame[12] == 1
    # 日程记录：slot, flags, start, end=start+3600
    assert frame[13] == 0 and frame[14] == 0
    assert struct.unpack_from(">I", frame, 15)[0] == 1_700_000_100
    assert struct.unpack_from(">I", frame, 19)[0] == 1_700_000_100 + 3600


def test_build_begin_rejects_out_of_range():
    with pytest.raises(ValueError):
        build_begin(transaction=0, now_utc=0, timezone_minutes=480)
    with pytest.raises(ValueError):
        build_begin(1, 0, 480, foods=[FoodRecord(i, 0, 0) for i in range(5)])


def test_bitmap_packets_single_chunk_with_crc():
    data = bytes([0b10101010, 0b01010101])  # 8x2 → 每行 1 字节
    packets = bitmap_packets(
        transaction=9, asset=0x10, width=8, height=2, data=data, max_write=20
    )
    assert len(packets) == 1
    pkt = packets[0]
    assert pkt[0:5] == bytes([0x42, 0x01, 0x09, 0x10, 0x03])  # 首片+末片
    assert struct.unpack_from(">H", pkt, 5)[0] == 8  # width
    assert pkt[7] == 2  # height
    assert struct.unpack_from(">H", pkt, 8)[0] == 2  # total
    assert struct.unpack_from(">H", pkt, 10)[0] == 0  # offset
    assert pkt[12:14] == data
    assert struct.unpack_from(">H", pkt, 14)[0] == 0xE5EA  # CRC(AA55)
    assert len(pkt) == 12 + 2 + 2


def test_bitmap_packets_multi_chunk_flags_and_single_crc():
    data = bytes(range(8))
    budget_max_write = 3 + 12 + 2  # 每片 3 字节数据
    packets = bitmap_packets(5, 0x11, 8, 8, data, budget_max_write)
    assert len(packets) == 3
    assert packets[0][4] == 0x01  # first
    assert packets[1][4] == 0x00
    assert packets[2][4] == 0x02  # last
    assert struct.unpack_from(">H", packets[1], 10)[0] == 3  # offset
    assert struct.unpack_from(">H", packets[2], 10)[0] == 6
    # 只有末片带 CRC
    assert len(packets[0]) == 12 + 3  # 非末片无 CRC
    assert len(packets[2]) == 12 + 2 + 2  # 末片带 CRC
    assert packets[2][12:14] == data[6:8]
    assert struct.unpack_from(">H", packets[2], 14)[0] == crc16_ccitt(data)


def test_bitmap_packets_rejects_bad_size():
    with pytest.raises(ValueError):
        bitmap_packets(1, 0x10, 152, 20, b"\x00" * 100, 200)
    with pytest.raises(ValueError):
        bitmap_packets(1, 0x10, 8, 1, b"", 200)
    with pytest.raises(ValueError):
        bitmap_packets(1, 0x10, 8, 1, b"\x00" * 2048, 200)


def test_commit_abort_sync_time_frames():
    assert build_commit(2) == bytes([0x43, 0x01, 0x02, 0x03])
    assert build_commit(2, flags=0x01) == bytes([0x43, 0x01, 0x02, 0x01])
    assert build_abort(3) == bytes([0x44, 0x01, 0x03])
    assert build_sync_time(4, 1000, 480) == bytes(
        [0x45, 0x01, 0x04, 0x00, 0x00, 0x03, 0xE8, 0x01, 0xE0]
    )


def test_parse_response_roundtrip():
    raw = bytes([0xC0, 0x01, 0x09, 0x42, 0x00]) + b"payload"
    resp = parse_response(raw)
    assert resp.ok
    assert resp.command == 0x42
    assert resp.transaction == 9
    assert resp.payload == b"payload"

    bad = parse_response(bytes([0xC0, 0x01, 0x01, 0x43, 0x07]))
    assert not bad.ok
    assert status_name(bad.status) == "BAD_CRC"
    with pytest.raises(ProtocolError):
        bad.raise_if_error()


def test_parse_response_garbage():
    with pytest.raises(ProtocolError):
        parse_response(bytes([0x01, 0x02]))


def test_parse_caps_response():
    payload = bytes(
        [
            0x1F,  # 固件 v1F
            0x01,  # 协议
            0x02,  # 日程上限
            0x04,  # 食品上限
        ]
    ) + struct.pack(">HHHHH", 1024, 0x003F, 244, 800, 480)
    resp = Response(protocol=1, transaction=0, command=0x40, status=0, payload=payload)
    caps = parse_caps_response(resp)
    assert caps.firmware == 0x1F
    assert caps.max_foods == 4
    assert caps.max_bitmap_bytes == 1024
    assert caps.max_data_len == 244
    assert caps.width == 800 and caps.height == 480


def test_parse_mtu_text():
    assert parse_mtu_text(b"mtu=247") == 247
    assert parse_mtu_text(b"mtu=247\n") == 247
    assert parse_mtu_text(b"hello") is None
    assert parse_mtu_text(b"mtu=") is None
