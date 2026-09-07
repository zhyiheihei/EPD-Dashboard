"""Secure DFU 引擎测试：以仿真 bootloader 逐字节验证传输流程。"""

import struct
import zlib

import pytest

from epd_food_server import dfu
from epd_food_server.dfu import (
    OBJECT_COMMAND,
    OBJECT_DATA,
    OP_CHECKSUM,
    OP_CREATE,
    OP_EXECUTE,
    OP_MTU,
    OP_PING,
    OP_PRN,
    OP_SELECT,
    SecureDfuSession,
)


class MockBootloaderTransport:
    """按 Nordic Secure DFU 规范仿真 bootloader 应答（控制点/数据点行为）。"""

    DATA_PAGE_SIZE = 4096
    COMMAND_MAX = 512
    NEGOTIATED_MTU = 247

    def __init__(self, fail_data_write: bool = False, execute_result: int | None = None):
        self.callback = None
        self.objects = {
            OBJECT_COMMAND: {"max": self.COMMAND_MAX, "size": 0, "data": bytearray(), "executed": False},
            OBJECT_DATA: {"max": self.DATA_PAGE_SIZE, "size": 0, "data": bytearray(), "executed": False},
        }
        self.create_calls = []
        self.executed_log = []
        self.data_written = bytearray()  # OBJECT_DATA 累计写入（跨页）
        self._since_prn = 0
        self.fail_data_write = fail_data_write  # 模拟丢包：丢弃每页最后一个字节
        self.execute_result = execute_result

    # ---- 传输接口 ----

    async def start_notify_control(self, callback) -> None:
        self.callback = callback

    def _respond(self, opcode: int, result: int, payload: bytes = b"") -> None:
        self.callback(bytearray(bytes([dfu.OP_RESPONSE, opcode, result]) + payload))

    async def write_control(self, data: bytes) -> None:
        op = data[0]
        if op == OP_PING:
            self._respond(OP_PING, 0x01, data[1:2])
        elif op == OP_MTU:
            self._respond(OP_MTU, 0x01, struct.pack("<H", self.NEGOTIATED_MTU))
        elif op == OP_PRN:
            self._respond(OP_PRN, 0x01)
        elif op == OP_SELECT:
            obj = self.objects[data[1]]
            self._respond(
                OP_SELECT,
                0x01,
                struct.pack("<III", obj["max"], len(obj["data"]), zlib.crc32(bytes(obj["data"]))),
            )
        elif op == OP_CREATE:
            obj_type = data[1]
            size = struct.unpack("<I", data[2:6])[0]
            obj = self.objects[obj_type]
            obj["size"] = size
            obj["data"] = bytearray()
            obj["executed"] = False  # 重新创建对象（如多页数据的第二页）
            self._since_prn = 0  # SDK 在 CREATE 时重置回执计数
            self.create_calls.append((obj_type, size))
            self._respond(OP_CREATE, 0x01)
        elif op == OP_CHECKSUM:
            key, current = self._current_object()
            assert current is not None, "CHECKSUM 无活动对象"
            self._respond(
                OP_CHECKSUM, 0x01,
                struct.pack("<II", len(current["data"]), zlib.crc32(bytes(current["data"]))),
            )
        elif op == OP_EXECUTE:
            key, current = self._current_object()
            if current is None:
                self._respond(OP_EXECUTE, 0x03)
            elif len(current["data"]) != current["size"]:
                self._respond(OP_EXECUTE, 0x03)
            elif self.execute_result is not None:
                self._respond(OP_EXECUTE, self.execute_result)
            else:
                current["executed"] = True
                self.executed_log.append(current["size"])
                self._respond(OP_EXECUTE, 0x01)
        else:
            self._respond(op, 0x02)

    async def write_data(self, data: bytes) -> None:
        key, current = self._current_object()
        if self.fail_data_write and len(current["data"]) + len(data) >= current["size"]:
            data = data[:-1]  # 丢最后一字节 → 校验必失败
        current["data"].extend(data)
        if key == OBJECT_DATA:
            self.data_written.extend(data)
        # 模拟设备 PRN 回执（每 10 包一次，格式 0x60 0x08 0x01 offset crc）
        self._since_prn += 1
        if self._since_prn >= 10:
            self._since_prn = 0
            self.callback(
                bytearray(
                    bytes([dfu.OP_RESPONSE, dfu.OP_PRN_NOTIFICATION, 0x01])
                    + struct.pack("<II", len(current["data"]), zlib.crc32(bytes(current["data"])))
                )
            )

    def _current_object(self):
        return next(
            ((k, o) for k, o in self.objects.items() if o["size"] > 0 and not o["executed"]),
            (None, None),
        )

    async def write_size(self) -> int:
        return self.NEGOTIATED_MTU - 3

    async def close(self) -> None:
        pass


def test_full_transfer_flow():
    """完整流程：init 包 + 9KB 应用（3 页数据对象），逐字节落位。"""
    transport = MockBootloaderTransport()
    dat = bytes(range(256)) * 2  # 512B init 包
    bin_data = bytes((i * 7 + 3) % 256 for i in range(9000))

    session = SecureDfuSession(transport, timeout=5.0)
    progress = []
    import asyncio

    asyncio.run(session.transfer(dat, bin_data, lambda done, total: progress.append((done, total))))

    # init 包对象精确传输并执行
    cmd = transport.objects[OBJECT_COMMAND]
    assert bytes(cmd["data"]) == dat and cmd["executed"]
    assert (OBJECT_COMMAND, len(dat)) in transport.create_calls

    # 数据按 4096 页创建并执行
    assert transport.create_calls.count((OBJECT_DATA, 4096)) == 2
    assert transport.create_calls.count((OBJECT_DATA, 808)) == 1  # 9000 = 4096*2 + 808
    assert bytes(transport.data_written) == bin_data  # 跨页累计内容逐字节一致
    assert len(transport.data_written) == 9000

    # 进度单调递增且终点为总量
    assert progress[-1] == (len(dat) + len(bin_data), len(dat) + len(bin_data))
    assert all(a <= b for (_, a), (_, b) in zip(progress, progress[1:]))


def test_crc_mismatch_detected():
    """传输丢包 → CHECKSUM 校验不一致 → 报错且不执行。"""
    transport = MockBootloaderTransport(fail_data_write=True)
    session = SecureDfuSession(transport, timeout=5.0)
    import asyncio

    with pytest.raises(dfu.DfuError, match="校验失败"):
        asyncio.run(session.transfer(b"\x01\x02\x03\x04", bytes(100)))
    # 失败后不应对错误对象执行
    assert all(not o["executed"] for o in transport.objects.values())


def test_execute_error_surfaces_result_name():
    transport = MockBootloaderTransport(execute_result=0x08)
    session = SecureDfuSession(transport, timeout=5.0)
    import asyncio

    with pytest.raises(dfu.DfuError, match="OPERATION_NOT_PERMITTED"):
        asyncio.run(session.transfer(b"\x01\x02\x03\x04", bytes(32)))


def test_init_packet_already_flashed_skips_create():
    """SELECT 报告 init 包已在位且 CRC 一致 → 跳过 init 重传。"""
    transport = MockBootloaderTransport()
    dat = b"\xAB" * 64
    # 预置：模拟上一次升级已写入 init 包
    transport.objects[OBJECT_COMMAND]["data"] = bytearray(dat)
    transport.objects[OBJECT_COMMAND]["size"] = len(dat)
    transport.objects[OBJECT_COMMAND]["executed"] = True

    session = SecureDfuSession(transport, timeout=5.0)
    import asyncio

    asyncio.run(session.transfer(dat, bytes(64)))

    assert (OBJECT_COMMAND, len(dat)) not in transport.create_calls
    assert (OBJECT_DATA, 64) in transport.create_calls


class NoPingTransport(MockBootloaderTransport):
    """模拟不支持 PING 的 SDK17 bootloader。"""

    async def write_control(self, data: bytes) -> None:
        if data[0] == OP_PING:
            self._respond(OP_PING, 0x02)  # OP_CODE_NOT_SUPPORTED
            return
        await super().write_control(data)


def test_ping_unsupported_is_tolerated():
    transport = NoPingTransport()
    session = SecureDfuSession(transport, timeout=5.0)
    import asyncio

    asyncio.run(session.transfer(b"\x01\x02", bytes(64)))
    assert bytes(transport.data_written) == bytes(64)
