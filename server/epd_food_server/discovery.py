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

# 固件在广播里用厂商数据（company id 0xFFFF）携带面板状态：首字节 bit0 =
# 「面板内容已丢失，需要服务端重推」（上电/重启/OTA 后置 1，COMMIT 成功后清 0）。
PANEL_STATE_COMPANY_ID = 0xFFFF


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
    """被动扫一下广播，读出「面板内容需要重推」标志（0xFFFF 厂商数据首字节）。

    只扫描不连接：设备上电/重启/OTA 后 RAM 里的面板内容就没了，固件把
    这个状态放在广播里，服务端定期扫到就知道该推了，不必为了探活去连设备。
    未找到设备返回 None（不在范围/已休眠）。
    """
    match = _match_app_device(cfg)
    found: list[tuple[str, dict]] = []

    def on_detect(device, advertisement) -> None:
        if found or not match(device, advertisement):
            return
        manuf = getattr(advertisement, "manufacturer_data", None) or {}
        payload = manuf.get(PANEL_STATE_COMPANY_ID, b"")
        found.append(
            (
                device.name or device.address,
                {
                    "needs_data": bool(payload and payload[0] & 1),
                    "manufacturer_data": payload.hex(),
                },
            )
        )

    try:
        scanner = BleakScanner(detection_callback=on_detect)
        await scanner.start()
        try:
            await asyncio.sleep(cfg.scan_timeout)
        finally:
            await scanner.stop()
    except Exception as exc:
        raise DeviceError(f"BLE 扫描失败: {exc}", stage="scan") from exc
    if not found:
        return None
    name, state = found[0]
    return {"device": name, **state}


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
