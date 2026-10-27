"""BLE 层错误类型：stage/status 字段由传输事务重抛时使用。"""

from epd_food_server.discovery import DeviceError
from epd_food_server.protocol import ProtocolError


def test_device_error_accepts_status_and_stage():
    # push_foods 收尾用 type(exc)(..., status=..., stage=...) 重抛，
    # DeviceError 少这个关键字就会把真实传输错误变成 TypeError。
    err = DeviceError("x", status=5, stage="commit")
    assert err.status == 5
    assert err.stage == "commit"
    assert DeviceError("y").status is None


def test_protocol_error_is_device_error_sibling():
    assert ProtocolError("x", status=1, stage="begin").status == 1


def test_panel_state_flag_parsing():
    """广播厂商数据首字节 bit0 = 面板内容丢失（需要服务端重推）。"""
    import asyncio

    from epd_food_server import discovery

    class FakeAdv:
        def __init__(self, manuf):
            self.manufacturer_data = manuf
            self.service_uuids = []

    class FakeDevice:
        address = "F7:F3:13:0B:3E:1C"
        name = "NRF_EPD_3E1C"

    class FakeScanner:
        payload = b"\x01"

        def __init__(self, detection_callback=None):
            self._cb = detection_callback

        async def start(self):
            self._cb(FakeDevice(), FakeAdv({0xFFFF: FakeScanner.payload}))

        async def stop(self):
            pass

    cfg = type("Cfg", (), {"device_address": "", "device_name_prefix": "NRF_EPD", "scan_timeout": 0.01})()
    original = discovery.BleakScanner
    discovery.BleakScanner = FakeScanner
    try:
        state = asyncio.run(discovery.read_panel_state(cfg))
        assert state == {"device": "NRF_EPD_3E1C", "needs_data": True, "manufacturer_data": "01"}

        FakeScanner.payload = b"\x00"
        state = asyncio.run(discovery.read_panel_state(cfg))
        assert state["needs_data"] is False

        # 没有厂商数据（旧固件）不能当成长了数据，也不能崩
        FakeScanner.payload = b""
        state = asyncio.run(discovery.read_panel_state(cfg))
        assert state["needs_data"] is False
    finally:
        discovery.BleakScanner = original
