"""设备发现与应用模式版本读取（独立于 GATT 会话的 BLE 入口层）。

扫描/过滤只看广播数据（名称前缀、指定地址、EPD 服务 UUID），不依赖
BlueZ 的 UUID 过滤器；版本读取带 TTL 缓存，避免反复连接唤醒设备。
"""

from __future__ import annotations

import asyncio
import time

from bleak import BleakClient, BleakScanner

from . import protocol
from .config import Config


class DeviceError(RuntimeError):
    """BLE 层错误（扫描不到设备、连接失败等），stage 标记失败阶段。"""

    def __init__(self, message: str, status: int | None = None, stage: str | None = None):
        super().__init__(message)
        self.status = status
        self.stage = stage


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
        raise DeviceError(f"BLE 扫描失败: {exc}", stage="scan") from exc
    if device is None:
        hint = (cfg.device_address or "").upper() or f"名称前缀 {cfg.device_name_prefix}"
        raise DeviceError(f"未找到墨水屏设备（{hint}），请确认设备已上电且在范围内", stage="scan")
    return device


async def read_panel_state(cfg: Config) -> dict | None:
    """连接一次读出面板状态块（含 boot_count），用于判断设备是否重启过。

    面板内容只在设备 RAM 里：上电/重启/OTA 后面板就空了，而服务端只知道
    「上次推送成功」。这里连一次、读 16 字节状态块、立即断开（不做任何
    图形传输，不扰动面板），拿 boot_count 跟上次记录比对即可发现重启。
    固件闪存区已贴顶（0xE000 预算，v29 = 0xDFE8），所以不在广播里加料。
    设备不在范围/未广播时返回 None。
    """
    from .ble import EpdSession

    try:
        async with EpdSession(cfg) as session:
            await session.handshake()
            status = await session.read_device_status()
            if status is None:
                return None
            return {"device": session.device_name, **status}
    except DeviceError as exc:
        if exc.stage in ("scan", "connect"):
            return None
        raise
    except Exception as exc:
        raise DeviceError(f"读取面板状态失败: {exc}", stage="status") from exc


_version_cache: tuple[float, str, int] | None = None
VERSION_CACHE_TTL = 300.0  # 版本读取需要 BLE 连接；设备不干活时应保持休眠，缓存 5 分钟


async def read_app_version(cfg: Config, max_age: float | None = None) -> tuple[str, int]:
    """读取固件版本特征（0x62750003，1 字节）；带 TTL 缓存避免反复 BLE 连接唤醒设备。"""
    global _version_cache
    ttl = VERSION_CACHE_TTL if max_age is None else max_age
    if _version_cache is not None and time.monotonic() - _version_cache[0] < ttl:
        return (_version_cache[1], _version_cache[2])
    device = await find_app_device(cfg)
    client = BleakClient(device, timeout=cfg.connect_timeout)
    try:
        await client.connect()
        data = await client.read_gatt_char(protocol.VERSION_CHARACTERISTIC_UUID)
        result = (device.name or device.address, data[0])
        _version_cache = (time.monotonic(), *result)
        return result
    except DeviceError:
        raise
    except Exception as exc:
        raise DeviceError(f"读取固件版本失败: {exc}") from exc
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
