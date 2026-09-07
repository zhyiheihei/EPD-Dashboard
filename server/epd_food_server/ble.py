"""bleak BLE 会话：扫描/连接/通知路由/看板协议事务流程。

服务端为 BLE Central（GATT 客户端），墨水屏为持续广播的外设（无配对加密）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Callable

from bleak import BleakClient, BleakScanner

from . import protocol
from .config import Config
from .protocol import (
    CapsInfo,
    FoodRecord,
    ProtocolError,
    Response,
    ScheduleRecord,
    parse_caps_response,
    parse_response,
)

log = logging.getLogger(__name__)

FALLBACK_MTU = 23  # ATT 默认 MTU；收不到固件 mtu= 文本时的兜底
MIN_MTU = 23


class DeviceError(RuntimeError):
    """BLE 层错误（扫描不到设备、连接失败等）。"""


def _match_app_device(cfg: Config):
    """构造应用模式设备过滤器（名称前缀 / 指定地址 / EPD 服务 UUID）。"""
    want_address = (cfg.device_address or "").replace("-", ":").upper() or None

    def match(device, advertisement) -> bool:
        if want_address and device.address.upper() == want_address:
            return True
        name = device.name or ""
        if cfg.device_name_prefix and name.startswith(cfg.device_name_prefix):
            return True
        uuids = [u.lower() for u in (getattr(advertisement, "service_uuids", None) or [])]
        return protocol.SERVICE_UUID.lower() in uuids

    return match


async def find_app_device(cfg: Config):
    try:
        # 不用 BlueZ 的 UUID 发现过滤器（某些 bluetoothd 状态下会报
        # "No discovery started"），靠广播数据里的名称/UUID 自行匹配。
        device = await BleakScanner.find_device_by_filter(
            _match_app_device(cfg), timeout=cfg.scan_timeout
        )
    except Exception as exc:
        raise DeviceError(f"BLE 扫描失败: {exc}") from exc
    if device is None:
        hint = (cfg.device_address or "").upper() or f"名称前缀 {cfg.device_name_prefix}"
        raise DeviceError(f"未找到墨水屏设备（{hint}），请确认设备已上电且在范围内")
    return device


async def read_app_version(cfg: Config) -> tuple[str, int]:
    """连接设备读取固件版本特征（0x62750003，1 字节），用于 OTA 前版本比对。"""
    device = await find_app_device(cfg)
    client = BleakClient(device, timeout=cfg.connect_timeout)
    try:
        await client.connect()
        data = await client.read_gatt_char(protocol.VERSION_CHARACTERISTIC_UUID)
        return (device.name or device.address, data[0])
    except DeviceError:
        raise
    except Exception as exc:
        raise DeviceError(f"读取固件版本失败: {exc}") from exc
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


class EpdSession:
    """一次到墨水屏的完整会话。用法：`async with EpdSession(cfg) as session:`"""

    def __init__(self, cfg: Config, on_log: Callable[[str], None] | None = None):
        self._cfg = cfg
        self._client: BleakClient | None = None
        self._device_name: str | None = None
        self._mtu: int = FALLBACK_MTU
        self._queues: dict[int, asyncio.Queue[Response]] = {}
        self._mtu_event: asyncio.Event = asyncio.Event()
        self._on_log = on_log or (lambda msg: None)

    # ---------- 生命周期 ----------

    async def __aenter__(self) -> "EpdSession":
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def open(self) -> None:
        device = await find_app_device(self._cfg)
        self._device_name = device.name or device.address
        self._log(f"连接设备 {self._device_name} ({device.address})")
        self._client = BleakClient(device, timeout=self._cfg.connect_timeout)
        try:
            await self._client.connect()
        except Exception as exc:
            raise DeviceError(f"连接 {self._device_name} 失败: {exc}") from exc
        try:
            await self._client.start_notify(protocol.CHARACTERISTIC_UUID, self._on_notify)
        except Exception as exc:
            await self.close()
            raise DeviceError(f"订阅通知失败: {exc}") from exc

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    @property
    def device_name(self) -> str | None:
        return self._device_name

    @property
    def mtu(self) -> int:
        return self._mtu

    @property
    def max_write(self) -> int:
        """特征单次写入上限（ATT MTU - 3），分片预算由此推算。"""
        return max(MIN_MTU, self._mtu) - 3

    # ---------- 通知路由 ----------

    def _log(self, msg: str) -> None:
        log.info("EPD %s: %s", self._device_name or "?", msg)

    def _on_notify(self, _char, data: bytearray) -> None:
        payload = bytes(data)
        if payload and payload[0] == protocol.CMD_RESPONSE:
            try:
                response = parse_response(payload)
            except ProtocolError as exc:
                self._log(f"异常通知: {exc}")
                return
            queue = self._queues.setdefault(response.command, asyncio.Queue())
            queue.put_nowait(response)
            return
        mtu = protocol.parse_mtu_text(payload)
        if mtu is not None:
            self._mtu = max(MIN_MTU, mtu)
            self._log(f"设备报告 MTU={self._mtu}")
            self._mtu_event.set()
            return
        if len(payload) == 13:
            self._log(f"收到 epd_config: {payload.hex()}")
            return
        self._log(f"文本通知: {payload.decode('utf-8', errors='ignore').strip()}")

    async def _wait_response(self, command: int, transaction: int, timeout: float) -> Response:
        queue = self._queues.setdefault(command, asyncio.Queue())
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeviceError(
                    f"等待命令 0x{command:02X} 应答超时（tx={transaction}, {timeout:.0f}s）"
                )
            try:
                response = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            if response.transaction != transaction:
                self._log(f"忽略过期应答 tx={response.transaction} cmd=0x{response.command:02X}")
                continue
            response.raise_if_error()
            return response

    async def _write(self, payload: bytes) -> None:
        assert self._client is not None
        try:
            await self._client.write_gatt_char(
                protocol.CHARACTERISTIC_UUID, payload, response=True
            )
        except Exception as exc:
            raise DeviceError(f"写入失败（len={len(payload)}）: {exc}") from exc

    # ---------- 协议流程 ----------

    async def handshake(self) -> CapsInfo:
        """INIT（获取 MTU）→ CAPS（校验协议与分辨率）。"""
        await self._write(bytes([protocol.CMD_INIT]))
        try:
            await asyncio.wait_for(self._mtu_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            bleak_mtu = getattr(self._client, "mtu_size", 0) or 0
            self._mtu = max(MIN_MTU, bleak_mtu)
            self._log(f"未收到设备 MTU 通知，回退 {self._mtu}")

        await self._write(protocol.build_caps_request())
        response = await self._wait_response(
            protocol.CMD_CAPS, transaction=0, timeout=self._cfg.session_timeout
        )
        caps = parse_caps_response(response)
        if caps.protocol != protocol.PROTOCOL_VERSION:
            raise DeviceError(
                f"协议版本不兼容: 设备最高 0x{caps.protocol:02X}，服务端 0x{protocol.PROTOCOL_VERSION:02X}"
            )
        if caps.width <= 0 or caps.height <= 0:
            raise DeviceError(f"设备报告的分辨率异常: {caps.width}x{caps.height}")
        self._log(
            f"CAPS: 固件 0x{caps.firmware:02X} {caps.width}x{caps.height} "
            f"食品上限 {caps.max_foods} 单包 {caps.max_data_len}B"
        )
        return caps

    async def push_foods(
        self,
        foods: list[FoodRecord],
        bitmaps: list[bytes],
        now_utc: int,
        timezone_minutes: int,
        schedules: list[ScheduleRecord] | None = None,
        week_start: int = 1,
        bitmap_size: tuple[int, int] = (
            protocol.FOOD_BITMAP_WIDTH,
            protocol.FOOD_BITMAP_HEIGHT,
        ),
    ) -> None:
        """BEGIN → 逐资源 BITMAP（串行，末片等 42 OK）→ COMMIT(03)。"""
        if len(foods) != len(bitmaps):
            raise ValueError("foods 与 bitmaps 数量不一致")
        transaction = random.randint(1, 255)
        begin = protocol.build_begin(
            transaction, now_utc, timezone_minutes, week_start, schedules, foods
        )
        await self._write(begin)
        await self._wait_response(
            protocol.CMD_BEGIN, transaction, self._cfg.session_timeout
        )

        width, height = bitmap_size
        max_write = self.max_write
        if self._cfg.max_chunk > 0:  # 调试/兼容：强制保守分片
            max_write = min(
                max_write,
                self._cfg.max_chunk + protocol.BITMAP_HEADER_LEN + protocol.BITMAP_CRC_LEN,
            )
        for record, bitmap in zip(foods, bitmaps):
            asset = protocol.FOOD_SLOT_BASE + record.slot
            packets = protocol.bitmap_packets(
                transaction, asset, width, height, bitmap, max_write
            )
            self._log(f"发送位图槽位 0x{asset:02X}（{len(bitmap)}B / {len(packets)} 包）")
            for index, packet in enumerate(packets):
                await self._write(packet)
                if index == len(packets) - 1:
                    await self._wait_response(
                        protocol.CMD_BITMAP, transaction, self._cfg.session_timeout
                    )

        await self._write(protocol.build_commit(transaction))
        await self._wait_response(
            protocol.CMD_COMMIT, transaction, self._cfg.session_timeout
        )
        self._log("COMMIT OK，设备开始刷新")

    async def abort(self, transaction: int) -> None:
        try:
            await self._write(protocol.build_abort(transaction))
        except DeviceError as exc:
            self._log(f"ABORT 发送失败（可忽略）: {exc}")
