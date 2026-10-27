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


def test_panel_state_reads_boot_count(monkeypatch):
    """watch 靠 boot_count 判断设备是否重启（面板内容只在 RAM）。"""
    import asyncio

    from epd_food_server import discovery

    class FakeSession:
        device_name = "NRF_EPD_3E1C"

        def __init__(self, cfg):
            self.device_status = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def handshake(self):
            return None

        async def read_device_status(self):
            return {"boot_count": 7, "uptime_s": 12}

    import epd_food_server.ble as ble_mod

    monkeypatch.setattr(ble_mod, "EpdSession", FakeSession)
    state = asyncio.run(discovery.read_panel_state(type("Cfg", (), {})()))
    assert state == {"device": "NRF_EPD_3E1C", "boot_count": 7, "uptime_s": 12}
