"""Nordic Secure DFU 客户端（bleak 传输 + 可仿真传输层）。

固件（EPD-nRF5）使用 SDK17 的无绑定 Buttonless DFU + Secure DFU bootloader：
1. 应用模式：向 Buttonless 特征（服务 8EC90000-…）写 0x01，设备指示应答后复位进 bootloader；
2. bootloader 模式：以 0000FE59-… 服务广播，控制点 8EC90001-…/数据点 8EC90002-… 走标准
   Secure DFU（init 包 dat + 应用 bin 分对象写入，CRC32 校验，逐对象 execute）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import struct
import time
import zlib
from typing import Callable, Protocol

from bleak import BleakClient, BleakScanner

from .config import Config

log = logging.getLogger(__name__)

# ---------- UUID ----------
# 注意：本固件（SDK17 ble_dfu_unbonded）的应用模式在 Secure DFU 服务（FE59）下暴露
# Buttonless 特征 8EC90003（write+indicate）；bootloader 模式的控制点/数据点为
# 8EC90001 / 8EC90002。真机 NRF_EPD 已按此实测核对。

BUTTONLESS_SERVICE_UUID = "0000fe59-0000-1000-8000-00805f9b34fb"
BUTTONLESS_CHAR_UUID = "8ec90003-f315-4f60-9fb8-838830daea50"
SECURE_DFU_SERVICE_UUID = "0000fe59-0000-1000-8000-00805f9b34fb"
DFU_CONTROL_POINT_UUID = "8ec90001-f315-4f60-9fb8-838830daea50"
DFU_DATA_POINT_UUID = "8ec90002-f315-4f60-9fb8-838830daea50"

# ---------- Secure DFU 操作码 ----------

OP_CREATE = 0x01
OP_PRN = 0x02
OP_CHECKSUM = 0x03
OP_EXECUTE = 0x04
OP_READ_ERROR = 0x05
OP_SELECT = 0x06
OP_PING = 0x07
OP_MTU = 0x08
OP_RESPONSE = 0x60

OBJECT_COMMAND = 0x01
OBJECT_DATA = 0x02

# 异步 Packet Receipt Notification 通知标识（0x60 0x08 0x01 offset crc）
OP_PRN_NOTIFICATION = 0x08
# 数据页校验失败时的最大重传次数
MAX_PAGE_ATTEMPTS = 3

RESULT_NAMES = {
    0x01: "SUCCESS",
    0x02: "OP_CODE_NOT_SUPPORTED",
    0x03: "INVALID_PARAM",
    0x04: "INSUFFICIENT_RESOURCES",
    0x05: "INVALID_OBJECT",
    0x06: "SIGNATURE_MISMATCH",
    0x07: "UNSUPPORTED_TYPE",
    0x08: "OPERATION_NOT_PERMITTED",
}

DEFAULT_WRITE_SIZE = 20  # 未知 MTU 时的保守写入大小
BOOTLOADER_SCAN_TIMEOUT = 30.0
BOOTLOADER_SCAN_NAMES = ("DfuTarg",)


class DfuError(RuntimeError):
    def __init__(self, message: str, result: int | None = None):
        super().__init__(message)
        self.result = result


# ---------- 传输层 ----------

class DfuTransport(Protocol):
    """传输抽象：bleak 真机实现或测试仿真实现。"""

    async def start_notify_control(self, callback: Callable[[bytearray], None]) -> None: ...
    async def write_control(self, data: bytes) -> None: ...
    async def write_data(self, data: bytes) -> None: ...
    async def write_size(self) -> int: ...  # 单包数据上限（已扣 3 字节头部）
    async def close(self) -> None: ...


class BleakDfuTransport(DfuTransport):
    def __init__(self, client: BleakClient, on_notify_raw: Callable[[bytes], None] | None = None):
        self._client = client
        self._on_notify: Callable[[bytearray], None] | None = None
        self._raw_sink = on_notify_raw
        self._write_size: int | None = None

    async def start_notify_control(self, callback: Callable[[bytearray], None]) -> None:
        self._on_notify = callback

        def _bleak_handler(_characteristic, data: bytearray) -> None:
            # bleak 通知回调签名是 (char, data)，适配为单参数
            if self._raw_sink is not None:
                self._raw_sink(bytes(data))
            callback(data)

        await self._client.start_notify(DFU_CONTROL_POINT_UUID, _bleak_handler)

    async def write_control(self, data: bytes) -> None:
        await self._client.write_gatt_char(DFU_CONTROL_POINT_UUID, data, response=True)

    async def write_data(self, data: bytes) -> None:
        await self._client.write_gatt_char(DFU_DATA_POINT_UUID, data, response=False)

    async def write_size(self) -> int:
        """协商 ATT MTU（bleak 私有接口，尽力而为），返回单包数据上限（MTU-3）。"""
        if self._write_size is None:
            mtu = 0
            backend = getattr(self._client, "_backend", None)
            acquire = getattr(backend, "_acquire_mtu", None)
            if acquire is not None:
                try:
                    await acquire()
                except Exception:
                    pass
            try:
                mtu = self._client.mtu_size or 0
            except Exception:
                mtu = 0
            self._write_size = max(20, mtu) - 3
        return self._write_size

    async def close(self) -> None:
        try:
            await self._client.disconnect()
        except Exception:
            pass


# ---------- Secure DFU 会话 ----------

class SecureDfuSession:
    """在给定传输层上执行标准 Secure DFU 传输流程。"""

    def __init__(
        self,
        transport: DfuTransport,
        timeout: float = 30.0,
        packet_delay: float = 0.008,
        on_log: Callable[[str], None] | None = None,
    ):
        self._transport = transport
        self._timeout = timeout
        self._packet_delay = packet_delay  # 写包间隔（s）：模拟底层发送背压，防丢包
        self._log_fn = on_log or (lambda msg: log.info("DFU: %s", msg))
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    def _log(self, msg: str) -> None:
        self._log_fn(msg)

    def _on_notify(self, data: bytearray) -> None:
        """控制点通知（bleak 同步回调）。"""
        if len(data) >= 3 and data[0] == OP_RESPONSE:
            self._queue.put_nowait(bytes(data))

    # ---------- 底层请求 ----------

    async def _request(self, opcode: int, payload: bytes = b"") -> bytes:
        self._queue = asyncio.Queue()  # 每请求新建，丢弃过期应答
        await self._transport.write_control(bytes([opcode]) + payload)
        deadline = time.monotonic() + self._timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DfuError(f"DFU 操作 0x{opcode:02X} 应答超时")
            try:
                data = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            if data[1] != opcode:
                self._log(f"忽略过期应答 op=0x{data[1]:02X}")
                continue
            result = data[2]
            if result != 0x01:
                name = RESULT_NAMES.get(result, f"RESULT_{result:02X}")
                raise DfuError(f"DFU 操作 0x{opcode:02X} 失败: {name}", result=result)
            return data[3:]

    # ---------- 协议原语 ----------

    async def ping(self) -> None:
        await self._request(OP_PING, bytes([random.randint(0, 255)]))

    async def set_prn(self, value: int = 0) -> None:
        await self._request(OP_PRN, struct.pack("<H", value))
    async def get_mtu(self) -> int | None:
        try:
            payload = await self._request(OP_MTU)
        except DfuError:
            return None
        if len(payload) >= 2:
            return struct.unpack("<H", payload[:2])[0]
        return None

    async def select(self, object_type: int) -> tuple[int, int, int]:
        """返回 (对象最大字节数, 已写偏移, CRC32)。"""
        payload = await self._request(OP_SELECT, bytes([object_type]))
        if len(payload) < 12:
            raise DfuError(f"SELECT 应答异常: {payload.hex()}")
        max_size, offset, crc = struct.unpack("<III", payload[:12])
        return max_size, offset, crc

    async def create(self, object_type: int, size: int) -> None:
        await self._request(OP_CREATE, bytes([object_type]) + struct.pack("<I", size))

    async def checksum(self) -> tuple[int, int]:
        payload = await self._request(OP_CHECKSUM)
        if len(payload) < 8:
            raise DfuError(f"CHECKSUM 应答异常: {payload.hex()}")
        offset, crc = struct.unpack("<II", payload[:8])
        return offset, crc

    async def execute(self) -> None:
        await self._request(OP_EXECUTE)

    # ---------- 传输流程 ----------

    async def _stream_object(self, payload: bytes, write_size: int) -> None:
        # bleak/BlueZ 无底层发送背压，包间加小延迟替代（对齐 nrfutil 默认 PRN=0 + 页尾校验）
        for offset in range(0, len(payload), write_size):
            await self._transport.write_data(payload[offset : offset + write_size])
            if self._packet_delay > 0:
                await asyncio.sleep(self._packet_delay)

    async def transfer(
        self,
        dat: bytes,
        bin_data: bytes,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """完整传输：init 包（命令对象）+ 应用（数据对象，按设备报告页大小分页）。"""
        await self._transport.start_notify_control(self._on_notify)
        try:
            await self.ping()
        except DfuError as exc:
            # SDK17 bootloader 不支持 PING(0x07)，可选步骤，跳过即可
            self._log(f"PING 被拒绝，跳过（{exc}）")
        await self.set_prn(0)  # 对齐 nrfutil 默认：关闭 PRN，页尾 checksum 兜底

        device_mtu = await self.get_mtu()
        transport_size = await self._transport.write_size()
        write_size = transport_size
        if device_mtu:
            write_size = min(write_size, max(20, device_mtu) - 3)
        self._log(f"DFU 写入大小 {write_size}B（设备 MTU={device_mtu}）")

        total = len(dat) + len(bin_data)
        done = 0

        # ---- init 包 ----
        max_size, offset, crc = await self.select(OBJECT_COMMAND)
        if offset == len(dat) and crc == zlib.crc32(dat):
            self._log("init 包已存在且校验一致，跳过重传")
        else:
            if max_size <= 0 or len(dat) > max_size:
                raise DfuError(f"init 包长度 {len(dat)} 超过对象上限 {max_size}")
            await self.create(OBJECT_COMMAND, len(dat))
            await self._stream_object(dat, write_size)
            got_offset, got_crc = await self.checksum()
            if (got_offset, got_crc) != (len(dat), zlib.crc32(dat)):
                raise DfuError(
                    f"init 包校验失败: 设备报 offset={got_offset} crc=0x{got_crc:08X}"
                )
            await self.execute()
        done += len(dat)
        if progress:
            progress(done, total)

        # ---- 应用数据 ----
        data_max, offset, _ = await self.select(OBJECT_DATA)
        if data_max <= 0:
            raise DfuError("设备报告数据对象大小为 0")
        offset = min(offset, len(bin_data))
        done = len(dat) + offset
        self._log(f"数据对象页大小 {data_max}B，共 {len(bin_data)}B，设备已收 {offset}B")
        if progress:
            progress(done, total)

        # 不同固件对 CHECKSUM 的语义可能是镜像累计偏移或对象内偏移，两者都接受
        while offset < len(bin_data):
            page_start = (offset // data_max) * data_max
            page_end = min(page_start + data_max, len(bin_data))
            page = bin_data[page_start:page_end]
            page_crc = zlib.crc32(page)
            image_crc = zlib.crc32(bin_data[:page_end])
            ok = False
            for attempt in range(1, MAX_PAGE_ATTEMPTS + 1):
                await self.create(OBJECT_DATA, len(page))
                await self._stream_object(page, write_size)
                got_offset, got_crc = await self.checksum()
                if (got_offset, got_crc) in ((len(page), page_crc), (page_end, image_crc)):
                    ok = True
                    break
                self._log(
                    f"页 @0x{page_start:X} 第 {attempt} 次校验不符（设备 offset={got_offset} "
                    f"crc=0x{got_crc:08X}），整页重传"
                )
            if not ok:
                raise DfuError(f"数据页 @0x{page_start:X} 重传 {MAX_PAGE_ATTEMPTS} 次仍校验失败")
            await self.execute()
            offset = page_end
            done = len(dat) + offset
            if progress:
                progress(done, total)
        self._log("全部对象传输并执行完毕")


# ---------- 设备发现与高层入口 ----------

async def enter_bootloader(cfg: Config) -> None:
    """连接应用模式设备，Buttonless 请求进入 bootloader。"""
    device = await _find_app_device(cfg)
    client = BleakClient(device, timeout=cfg.connect_timeout)
    try:
        await client.connect()
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise DfuError(f"连接设备超时（{device.name or device.address}）") from exc
    try:
        response: asyncio.Queue[bytearray] = asyncio.Queue()

        def on_indication(_char, data: bytearray) -> None:
            response.put_nowait(bytes(data))

        await client.start_notify(BUTTONLESS_CHAR_UUID, on_indication)
        log.info("已连接 %s，发送 Buttonless DFU 进入 bootloader", device.name or device.address)
        await client.write_gatt_char(BUTTONLESS_CHAR_UUID, bytes([0x01]), response=True)
        try:
            indication = await asyncio.wait_for(response.get(), timeout=5.0)
            log.info("设备指示应答: %s，即将复位", indication.hex())
        except asyncio.TimeoutError:
            log.warning("未收到 Buttonless 指示应答（部分固件直接复位）")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _find_app_device(cfg: Config):
    want_address = (cfg.device_address or "").replace("-", ":").upper() or None

    def match(device, advertisement) -> bool:  # noqa: ANN001
        if want_address and device.address.upper() == want_address:
            return True
        name = device.name or ""
        return bool(cfg.device_name_prefix) and name.startswith(cfg.device_name_prefix)

    device = await BleakScanner.find_device_by_filter(
        match, timeout=cfg.scan_timeout, service_uuids=None
    )
    if device is None:
        raise DfuError(
            f"未找到墨水屏设备（{want_address or f'名称前缀 {cfg.device_name_prefix}'}）"
        )
    return device


async def find_bootloader(cfg: Config, timeout: float | None = None):
    """扫描 bootloader 广播（FE59 服务或 DfuTarg 名称），返回设备或 None。"""

    def match(device, advertisement) -> bool:  # noqa: ANN001
        name = device.name or ""
        if name in BOOTLOADER_SCAN_NAMES:
            return True
        uuids = [u.lower() for u in (getattr(advertisement, "service_uuids", None) or [])]
        return SECURE_DFU_SERVICE_UUID in uuids

    deadline = time.monotonic() + (timeout or BOOTLOADER_SCAN_TIMEOUT)
    while time.monotonic() < deadline:
        device = await BleakScanner.find_device_by_filter(match, timeout=5.0)
        if device is not None:
            return device
    return None


async def _connect_verified_bootloader(device) -> BleakClient:
    """连接候选设备并确认它是真 bootloader（有控制点 8EC90001）。

    应用模式同样会广播 FE59（unbonded buttonless 服务挂在 FE59 下），只认广播会误连。
    """
    client = BleakClient(device, timeout=8.0)
    try:
        await client.connect()
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise DfuError(f"连接 bootloader 超时（{device.name or device.address}）") from exc
    uuids = {c.uuid for s in client.services for c in s.characteristics}
    if DFU_CONTROL_POINT_UUID not in uuids:
        await client.disconnect()
        raise DfuError(f"{device.name or device.address} 不是 bootloader（无控制点特征）")
    return client


async def upgrade_on_device(
    cfg: Config,
    dat: bytes,
    bin_data: bytes,
    progress: Callable[[int, int], None] | None = None,
) -> str:
    """完整真机流程：Buttonless 进 bootloader → Secure DFU 传输。返回 bootloader 设备名。"""
    # 上次升级失败可能让设备停在 bootloader 模式——先探测，避免误走 Buttonless
    bl_device = await find_bootloader(cfg, timeout=6.0)
    client: BleakClient | None = None
    if bl_device is not None:
        try:
            client = await _connect_verified_bootloader(bl_device)
        except (DfuError, Exception) as exc:  # noqa: BLE001 — 候选不是 bootloader 则走正常入口
            log.info("广播候选 %s 非 bootloader（%s），改走 Buttonless 入口", bl_device.address, exc)
            bl_device = None
    if client is None:
        await enter_bootloader(cfg)
        await asyncio.sleep(0.5)
        bl_device = await find_bootloader(cfg)
        if bl_device is None:
            raise DfuError("Buttonless 请求后未发现 DFU bootloader 广播")
        client = await _connect_verified_bootloader(bl_device)
    log.info("发现 bootloader: %s (%s)", bl_device.name or "?", bl_device.address)

    def raw_notify_sink(data: bytes) -> None:
        log.debug("DFU 通知: %s", data.hex())

    try:
        await client.connect()
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise DfuError(f"连接 bootloader 超时（{bl_device.name or bl_device.address}）") from exc
    transport = BleakDfuTransport(client, on_notify_raw=raw_notify_sink)
    try:
        session = SecureDfuSession(transport, timeout=cfg.session_timeout)
        await session.transfer(dat, bin_data, progress)
    finally:
        await transport.close()
    return bl_device.name or bl_device.address
