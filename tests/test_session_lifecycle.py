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
