"""推送编排策略测试：全刷决策、成功后落盘、传输失败暂停与手动恢复。

通过 monkeypatch 掉 BLE/渲染等重依赖，只验证 Pusher 的决策与状态机。
"""

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "server")

from epd_food_server import protocol
from epd_food_server.ble import DeviceError
from epd_food_server.pusher import Pusher, PushStatus


def make_pusher(state_dir: Path, **extra) -> Pusher:
    pusher = object.__new__(Pusher)
    fields = {
        "state_dir": state_dir,
        "lock_path": state_dir / "push.lock",
        "status_path": state_dir / "push-status.json",
        "settle_seconds": 0,
        "push_retries": 0,
        "retry_backoff": 0,
        "caldav_enabled": False,
        "timezone": "Asia/Shanghai",
        "commit_sleep": True,
    }
    fields.update(extra)
    pusher._cfg = type("Cfg", (), fields)()
    pusher._busy = asyncio.Lock()
    pusher._in_progress = False
    return pusher


class FakeDB:
    def top_for_display(self, limit: int):
        return [
            {"name": f"食物{i}", "category": "食品", "expiry_date": datetime.now().date()}
            for i in range(limit)
        ]


def install_stubs(monkeypatch, pusher: Pusher, run_session):
    """替换 DB / 渲染 / 日程 / BLE 会话，让 _push_blocking 跑纯决策逻辑。"""
    monkeypatch.setattr(Pusher, "_build_entries", lambda self, rows: ([], []))
    monkeypatch.setattr(Pusher, "_fetch_schedules", lambda self: ([], []))
    monkeypatch.setattr(Pusher, "_run_session", run_session)
    monkeypatch.setattr(Pusher, "_settle", lambda self, seconds=None: None)


def test_push_blocking_full_decision_and_persist_on_success(monkeypatch, tmp_path):
    """0 点 timer（当天首推）→ 全刷；成功后才落盘全刷日期与日程指纹。"""
    seen = {}
    state_dir = tmp_path / "state"
    pusher = make_pusher(state_dir)
    pusher._db = FakeDB()

    def run_session(self, foods, bitmaps, schedules, schedule_bitmaps, force_full):
        seen["force_full"] = force_full
        return "NRF_EPD_3E1C"

    install_stubs(monkeypatch, pusher, run_session)
    status = asyncio.run(pusher.push(reason="timer"))
    assert status.ok is True
    assert seen["force_full"] is True  # 当天首推全刷（含新倒计时与日程）
    today = datetime.now(timezone.utc).astimezone().date().isoformat()
    assert (state_dir / "last-full-refresh.txt").read_text() == today
    assert (state_dir / "last-schedules.txt").exists()

    # 当天后续推送（手动变更）→ 局部刷新
    status2 = asyncio.run(pusher.push(reason="change"))
    assert status2.ok is True
    assert seen["force_full"] is False
    assert (state_dir / "last-full-refresh.txt").read_text() == today


def test_push_failure_persists_decision_for_retry(monkeypatch, tmp_path):
    """全刷决策在尝试前定死；失败不落盘，下次推送重试同样的全刷。"""
    state_dir = tmp_path / "state"
    pusher = make_pusher(state_dir)
    pusher._db = FakeDB()
    calls = []

    def run_session(*args):
        calls.append(args[-1])
        raise DeviceError("未找到墨水屏设备", stage="scan")

    install_stubs(monkeypatch, pusher, run_session)
    status = asyncio.run(pusher.push(reason="timer"))
    assert status.ok is False
    assert status.stage == "scan"
    assert calls == [True]  # retries=0，一次尝试
    assert not (state_dir / "last-full-refresh.txt").exists()
    assert not (state_dir / "last-schedules.txt").exists()


def test_transfer_failure_pauses_automated_pushes(monkeypatch, tmp_path):
    """传输失败 → 持久化失败记录 + 暂停自动推送；手动推送成功后恢复。"""
    state_dir = tmp_path / "state"
    pusher = make_pusher(state_dir)
    pusher._db = FakeDB()
    install_stubs(
        monkeypatch, pusher,
        lambda *args: (_ for _ in ()).throw(DeviceError("COMMIT 超时", stage="commit")),
    )
    status = asyncio.run(pusher.push(reason="timer"))
    assert status.ok is False and status.stage == "commit"
    paused = json.loads((state_dir / "push-paused.json").read_text())
    assert paused["stage"] == "commit"
    assert json.loads((state_dir / "last-failure.json").read_text())["stage"] == "commit"

    # timer / change 被暂停拦截，不再触碰设备
    def must_not_run(*args):
        raise AssertionError("暂停期间不应再跑会话")

    install_stubs(monkeypatch, pusher, must_not_run)
    skipped = asyncio.run(pusher.push(reason="timer"))
    assert skipped.ok is False
    assert "暂停" in (skipped.error or "")

    # 手动推送放行，成功后解除暂停并清失败记录
    install_stubs(
        monkeypatch, pusher,
        lambda *args: "NRF_EPD_3E1C",
    )
    resumed = asyncio.run(pusher.push(reason="manual"))
    assert resumed.ok is True
    assert not (state_dir / "push-paused.json").exists()
    assert not (state_dir / "last-failure.json").exists()
    assert pusher._read_paused() is None
    # 解除后自动推送恢复
    assert asyncio.run(pusher.push(reason="change")).ok is True


def test_prepare_failure_does_not_pause(monkeypatch, tmp_path):
    """数据库/渲染等准备阶段失败不代表设备不可达，不应暂停自动推送。"""
    state_dir = tmp_path / "state"
    pusher = make_pusher(state_dir)
    pusher._db = type("BadDB", (), {"top_for_display": lambda self, limit: (_ for _ in ()).throw(RuntimeError("pg 挂了"))})()

    def must_not_run(*args):
        raise AssertionError("准备阶段失败不应触碰设备")

    install_stubs(monkeypatch, pusher, must_not_run)
    status = asyncio.run(pusher.push(reason="timer"))
    assert status.ok is False and status.stage == "prepare"
    assert not (state_dir / "push-paused.json").exists()


def test_read_status_includes_paused(monkeypatch, tmp_path):
    pusher = make_pusher(tmp_path / "state")
    assert pusher.read_status().get("paused") is None
    pusher._pause_automated(PushStatus(ok=False, reason="timer", error="x", stage="commit"))
    assert pusher.read_status()["paused"]["stage"] == "commit"


def test_serve_push_forces_full_refresh(monkeypatch, tmp_path):
    """服务启动推送（reason=serve, force_full=True）：当天已全刷仍强制全刷，
    且非 manual reason 仍受推送暂停约束。"""
    seen = {}
    state_dir = tmp_path / "state"
    pusher = make_pusher(state_dir)
    pusher._db = FakeDB()

    def run_session(self, foods, bitmaps, schedules, schedule_bitmaps, force_full):
        seen["force_full"] = force_full
        return "NRF_EPD_3E1C"

    install_stubs(monkeypatch, pusher, run_session)
    # 先以 timer 全刷并落盘，模拟当天已完成的全刷
    assert asyncio.run(pusher.push(reason="timer")).ok is True
    status = asyncio.run(pusher.push(reason="serve", force_full=True))
    assert status.ok is True
    assert seen["force_full"] is True

    # serve 推送不带 force_full 时沿用常规决策（当日已刷 → 局部）
    status2 = asyncio.run(pusher.push(reason="serve"))
    assert status2.ok is True
    assert seen["force_full"] is False

    # serve 处于暂停状态时被拦下（非 manual）
    install_stubs(
        monkeypatch, pusher,
        lambda *args: (_ for _ in ()).throw(DeviceError("COMMIT 超时", stage="commit")),
    )
    failed = asyncio.run(pusher.push(reason="timer"))
    assert failed.ok is False
    install_stubs(monkeypatch, pusher, run_session)
    paused = asyncio.run(pusher.push(reason="serve", force_full=True))
    assert paused.ok is False
    assert "暂停" in (paused.error or "")
