"""EPD 看板协议 v1 纯函数层。

依据：EPD-nRF5/docs/dashboard-protocol-v1.md（固件 v0x1F）与网页参考实现 html/js/main.js。
多字节整数一律大端；帧 = 命令字节 + [0x01 协议版本, ...]。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SERVICE_UUID = "62750001-d828-918d-fb46-b6c11c675aec"
CHARACTERISTIC_UUID = "62750002-d828-918d-fb46-b6c11c675aec"
VERSION_CHARACTERISTIC_UUID = "62750003-d828-918d-fb46-b6c11c675aec"

PROTOCOL_VERSION = 0x01

CMD_INIT = 0x01  # 旧命令，连接后发送一次，固件回文本 mtu=<N>
CMD_CAPS = 0x40
CMD_BEGIN = 0x41
CMD_BITMAP = 0x42
CMD_COMMIT = 0x43
CMD_ABORT = 0x44
CMD_SYNC_TIME = 0x45
CMD_RESPONSE = 0xC0

STATUS_OK = 0x00
STATUS_NAMES = {
    0x00: "OK",
    0x01: "BAD_VERSION",
    0x02: "BAD_LENGTH",
    0x03: "BAD_STATE",
    0x04: "BAD_TRANSACTION",
    0x05: "BAD_SLOT",
    0x06: "BAD_BITMAP",
    0x07: "BAD_CRC",
}

# 位图资源槽位：日程标题 0x00/0x01；食品名称 0x10–0x13
FOOD_SLOT_BASE = 0x10
MAX_FOODS = 4
MAX_SCHEDULES = 2

# 推荐尺寸（与固件网页布局一致）
FOOD_BITMAP_WIDTH = 152
FOOD_BITMAP_HEIGHT = 20

# COMMIT 标志
COMMIT_REFRESH = 0x01
COMMIT_SLEEP_AFTER = 0x02
COMMIT_DEFAULT = COMMIT_REFRESH | COMMIT_SLEEP_AFTER  # 0x03

# BITMAP 头 12 字节 + 末片 CRC 2 字节
BITMAP_HEADER_LEN = 12
BITMAP_CRC_LEN = 2

# 食品记录类型
FOOD_TYPE_FOOD = 0
FOOD_TYPE_DRINK = 1


class ProtocolError(RuntimeError):
    """协议层错误（含设备返回的非 OK 状态码）。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE：poly 0x1021，初值 0xFFFF，不反射，xor-out 0。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def status_name(status: int) -> str:
    return STATUS_NAMES.get(status, f"STATUS_{status:02X}")


@dataclass(frozen=True)
class Response:
    protocol: int
    transaction: int
    command: int
    status: int
    payload: bytes

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def raise_if_error(self) -> "Response":
        if not self.ok:
            raise ProtocolError(
                f"设备返回 {status_name(self.status)}（cmd=0x{self.command:02X} tx={self.transaction}）",
                status=self.status,
            )
        return self


def parse_response(data: bytes) -> Response:
    if len(data) < 5 or data[0] != CMD_RESPONSE:
        raise ProtocolError(f"无法解析设备响应: {data.hex()}")
    return Response(
        protocol=data[1],
        transaction=data[2],
        command=data[3],
        status=data[4],
        payload=bytes(data[5:]),
    )


@dataclass(frozen=True)
class CapsInfo:
    firmware: int
    protocol: int
    max_schedules: int
    max_foods: int
    max_bitmap_bytes: int
    features: int
    max_data_len: int
    width: int
    height: int


def build_caps_request() -> bytes:
    """请求包必须恰好为 `40 01`。"""
    return bytes([CMD_CAPS, PROTOCOL_VERSION])


def parse_caps_response(resp: Response) -> CapsInfo:
    if len(resp.payload) < 14:
        raise ProtocolError(f"CAPS 载荷不足 14 字节: {resp.payload.hex()}")
    p = resp.payload
    return CapsInfo(
        firmware=p[0],
        protocol=p[1],
        max_schedules=p[2],
        max_foods=p[3],
        max_bitmap_bytes=struct.unpack_from(">H", p, 4)[0],
        features=struct.unpack_from(">H", p, 6)[0],
        max_data_len=struct.unpack_from(">H", p, 8)[0],
        width=struct.unpack_from(">H", p, 10)[0],
        height=struct.unpack_from(">H", p, 12)[0],
    )


@dataclass(frozen=True)
class FoodRecord:
    slot: int  # 0–3
    type: int  # 0=食品 1=饮品
    expiry_utc: int  # 到期日当天 23:59:59（本地时区）的 UTC 秒


@dataclass(frozen=True)
class ScheduleRecord:
    slot: int  # 0–1
    start_utc: int


def build_begin(
    transaction: int,
    now_utc: int,
    timezone_minutes: int,
    week_start: int = 1,
    schedules: list[ScheduleRecord] | None = None,
    foods: list[FoodRecord] | None = None,
) -> bytes:
    """`41 01 tx flags utc(4) tz(2) week_start sched_cnt food_cnt [日程×10B] [食品×6B]`"""
    schedules = schedules or []
    foods = foods or []
    if not 1 <= transaction <= 255:
        raise ValueError("事务 ID 必须为 1–255")
    if len(schedules) > MAX_SCHEDULES:
        raise ValueError(f"日程数超过上限 {MAX_SCHEDULES}")
    if len(foods) > MAX_FOODS:
        raise ValueError(f"食品数超过上限 {MAX_FOODS}")
    out = bytearray([CMD_BEGIN, PROTOCOL_VERSION, transaction, 0x00])
    out += struct.pack(">I", now_utc & 0xFFFFFFFF)
    out += struct.pack(">H", timezone_minutes & 0xFFFF)
    out.append(week_start)
    out.append(len(schedules))
    out.append(len(foods))
    for item in schedules:
        out += bytes([item.slot, 0x00])
        out += struct.pack(">I", item.start_utc & 0xFFFFFFFF)
        out += struct.pack(">I", (item.start_utc + 3600) & 0xFFFFFFFF)
    for item in foods:
        out += bytes([item.slot, item.type])
        out += struct.pack(">I", item.expiry_utc & 0xFFFFFFFF)
    expected = 13 + len(schedules) * 10 + len(foods) * 6
    if len(out) != expected:
        raise AssertionError(f"BEGIN 帧长度 {len(out)} != 预期 {expected}")
    return bytes(out)


def bitmap_packets(
    transaction: int,
    asset: int,
    width: int,
    height: int,
    data: bytes,
    max_write: int,
) -> list[bytes]:
    """切分一个位图资源为 BITMAP 包序列，末片自动附带 CRC。

    `max_write` 为特征单次写入上限（ATT MTU-3）；每片数据 ≤ max_write-12-2。
    """
    total = len(data)
    if total == 0:
        raise ValueError("位图数据为空")
    if total > 1024:
        raise ValueError(f"位图超过固件上限 1024 字节: {total}")
    row_bytes = (width + 7) // 8
    if row_bytes * height != total:
        raise ValueError(f"位图尺寸不符: {width}x{height} 应为 {row_bytes * height} 字节, 实际 {total}")
    budget = max_write - BITMAP_HEADER_LEN - BITMAP_CRC_LEN
    if budget < 1:
        raise ValueError(f"MTU 过小，无法承载位图分片: max_write={max_write}")
    crc = crc16_ccitt(data)
    packets: list[bytes] = []
    offset = 0
    while offset < total:
        chunk = data[offset : offset + budget]
        end = offset + len(chunk)
        first = offset == 0
        last = end == total
        flags = (0x01 if first else 0x00) | (0x02 if last else 0x00)
        pkt = bytearray([CMD_BITMAP, PROTOCOL_VERSION, transaction, asset, flags])
        pkt += struct.pack(">H", width)
        pkt.append(height & 0xFF)
        pkt += struct.pack(">H", total)
        pkt += struct.pack(">H", offset)
        pkt += chunk
        if last:
            pkt += struct.pack(">H", crc)
        packets.append(bytes(pkt))
        offset = end
    return packets


def build_commit(transaction: int, flags: int = COMMIT_DEFAULT) -> bytes:
    return bytes([CMD_COMMIT, PROTOCOL_VERSION, transaction, flags])


def build_abort(transaction: int) -> bytes:
    return bytes([CMD_ABORT, PROTOCOL_VERSION, transaction])


def build_sync_time(transaction: int, now_utc: int, timezone_minutes: int) -> bytes:
    out = bytearray([CMD_SYNC_TIME, PROTOCOL_VERSION, transaction])
    out += struct.pack(">I", now_utc & 0xFFFFFFFF)
    out += struct.pack(">H", timezone_minutes & 0xFFFF)
    return bytes(out)


def parse_mtu_text(data: bytes) -> int | None:
    """INIT 后固件以文本通知 `mtu=<N>`，解析失败返回 None。"""
    try:
        text = data.decode("utf-8", errors="ignore").strip()
    except Exception:
        return None
    if text.startswith("mtu=") and text[4:].isdigit():
        return int(text[4:])
    return None
