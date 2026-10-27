"""BLE 会话生命周期：事务结束立刻断开，不占着设备等屏幕刷新。"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "server")

from epd_food_server import protocol
from epd_food_server.pusher import Pusher

from test_push_policy import make_pusher


class FakeSession:
    """记录会话进入/退出与事务调用的顺序。"""

    events: list[str] = []

    def __init__(self, cfg):
        self.device_name = "NRF_EPD_TEST"
        self.device_status = None
        self._caps = protocol.CapsInfo(
            firmware=0x28,
            protocol=1,
            max_schedules=8,
            max_foods=4,
            max_bitmap_bytes=1024,
            features=protocol.FEATURE_PARTIAL_REFRESH,
            max_data_len=244,
            width=800,
            height=480,
        )

    async def __aenter__(self):
        self.events.append("connect")
        return self

    async def __aexit__(self, *exc):
        self.events.append("disconnect")

    async def handshake(self):
        self.events.append("handshake")
        return self._caps

    async def read_device_status(self):
        self.events.append("status")
        return None

    async def push_foods(self, *args, **kwargs):
        self.events.append("push")


def test_session_closes_before_settle(monkeypatch, tmp_path):
    pusher = make_pusher(tmp_path / "state")
    monkeypatch.setattr("epd_food_server.pusher.EpdSession", FakeSession)
    monkeypatch.setattr(Pusher, "_settle", lambda self, seconds=None: FakeSession.events.append("settle"))

    FakeSession.events = []
    device, status = pusher._run_session(
        foods=[], bitmaps=[], schedules=[], schedule_bitmaps=[], force_full_refresh=True
    )

    assert device == "NRF_EPD_TEST"
    assert status is None
    # 关键顺序：settle（等屏幕刷新）发生在断开之后
    assert FakeSession.events == [
        "connect",
        "handshake",
        "status",
        "push",
        "disconnect",
        "settle",
    ]


def test_device_status_property_exposes_last_read():
    """read_device_status 的结果必须挂到 session 上：推送成功后要拿 boot_count
    判断设备是否重启过（否则每次 watch 都以为设备重启了，每 5 分钟推一次）。"""
    from epd_food_server.ble import EpdSession

    session = object.__new__(EpdSession)
    session._device_status = None
    session._stage = None
    session._log = lambda *a, **k: None
    session._write = _async_none
    session._wait_response = _async_status

    import asyncio

    status = asyncio.run(EpdSession.read_device_status(session))
    assert status["boot_count"] == 3
    assert session.device_status == status


async def _async_none(*args, **kwargs):
    return None


async def _async_status(*args, **kwargs):
    from epd_food_server import protocol

    payload = bytes.fromhex("290100030011 1a000000000200031307".replace(" ", ""))
    return protocol.Response(protocol=1, transaction=0, command=protocol.CMD_STATUS, status=0, payload=payload)
