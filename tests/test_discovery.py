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
