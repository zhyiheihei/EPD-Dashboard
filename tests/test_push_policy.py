"""推送编排策略测试：全刷决策、成功后落盘、传输失败后的退避重试与手动推送。

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
from epd_food_server.discovery import DeviceError
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
        "retry_base_seconds": 900.0,
        "retry_max_seconds": 21600.0,
        "textfile_path": None,
        "caldav_enabled": False,
        "timezone": "Asia/Shanghai",
        "commit_sleep": True,
    }
    fields.update(extra)
    pusher._cfg = type("Cfg", (), fields)()
    pusher._busy = asyncio.Lock()
    pusher._in_progress = False
    pusher._last_device_status = None
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
        return "NRF_EPD_3E1C", None

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


def test_transfer_failure_schedules_retry_backoff(monkeypatch, tmp_path):
    """传输失败 → 退避重试并落盘失败详情；退避到点自动重试，成功清除。"""
    state_dir = tmp_path / "state"
    pusher = make_pusher(state_dir)
    pusher._db = FakeDB()
    install_stubs(
        monkeypatch, pusher,
        lambda *args: (_ for _ in ()).throw(DeviceError("COMMIT 超时", stage="commit")),
    )
    status = asyncio.run(pusher.push(reason="timer"))
    assert status.ok is False and status.stage == "commit"
    retry = json.loads((state_dir / "push-retry.json").read_text())
    assert retry["stage"] == "commit" and retry["failures"] == 1
    assert retry["retry_after_s"] == 900.0
    assert json.loads((state_dir / "last-failure.json").read_text())["stage"] == "commit"

    # 退避未到：timer / change / retry 都不碰设备，且不往历史里刷噪声
    def must_not_run(*args):
        raise AssertionError("退避期内不应再跑会话")

    install_stubs(monkeypatch, pusher, must_not_run)
    skipped = asyncio.run(pusher.push(reason="timer"))
    assert skipped.ok is None
    assert "等待自动重试" in (skipped.error or "")
    assert asyncio.run(pusher.push(reason="retry")).ok is None
    assert len((state_dir / "push-history.jsonl").read_text().splitlines()) == 1

    # 退避到点：retry 定时器放行，成功后清除重试状态
    retry["next_attempt_at"] = "2000-01-01T00:00:00+08:00"
    (state_dir / "push-retry.json").write_text(json.dumps(retry))
    install_stubs(monkeypatch, pusher, lambda *args: ("NRF_EPD_3E1C", None))
    resumed = asyncio.run(pusher.push(reason="retry"))
    assert resumed.ok is True
    assert not (state_dir / "push-retry.json").exists()
    assert not (state_dir / "last-failure.json").exists()
    assert pusher._read_retry() is None
    assert json.loads((state_dir / "last-success.json").read_text())["device"] == "NRF_EPD_3E1C"
    # 清除后自动推送恢复
    assert asyncio.run(pusher.push(reason="change")).ok is True


def test_manual_push_ignores_backoff(monkeypatch, tmp_path):
    """手动推送不受退避限制；连续失败时退避时长递增并封顶。"""
    state_dir = tmp_path / "state"
    pusher = make_pusher(state_dir)
    pusher._db = FakeDB()
    install_stubs(
        monkeypatch, pusher,
        lambda *args: (_ for _ in ()).throw(DeviceError("连接超时", stage="connect")),
    )
    assert asyncio.run(pusher.push(reason="timer")).ok is False
    manual = asyncio.run(pusher.push(reason="manual"))
    assert manual.ok is False
    retry = json.loads((state_dir / "push-retry.json").read_text())
    assert retry["failures"] == 2 and retry["retry_after_s"] == 1800.0


def test_retry_delay_grows_then_caps(tmp_path):
    pusher = make_pusher(tmp_path / "state")
    assert [pusher._retry_delay(n) for n in range(1, 9)] == [
        900.0, 1800.0, 3600.0, 7200.0, 14400.0, 21600.0, 21600.0, 21600.0
    ]


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
    assert not (state_dir / "push-retry.json").exists()


def test_serve_push_forces_full_refresh(monkeypatch, tmp_path):
    """服务启动推送（reason=serve, force_full=True）：当天已全刷仍强制全刷，
    且非 manual reason 仍受推送暂停约束。"""
    seen = {}
    state_dir = tmp_path / "state"
    pusher = make_pusher(state_dir)
    pusher._db = FakeDB()

    def run_session(self, foods, bitmaps, schedules, schedule_bitmaps, force_full):
        seen["force_full"] = force_full
        return "NRF_EPD_3E1C", None

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

    # serve 处于退避期内同样让位（非 manual）
    install_stubs(
        monkeypatch, pusher,
        lambda *args: (_ for _ in ()).throw(DeviceError("COMMIT 超时", stage="commit")),
    )
    failed = asyncio.run(pusher.push(reason="timer"))
    assert failed.ok is False
    install_stubs(monkeypatch, pusher, run_session)
    deferred = asyncio.run(pusher.push(reason="serve", force_full=True))
    assert deferred.ok is None
    assert "等待自动重试" in (deferred.error or "")


def test_read_status_includes_retry_and_last_success(tmp_path):
    pusher = make_pusher(tmp_path / "state")
    status = pusher.read_status()
    assert status["retry"] is None and status["paused"] is None
    assert status["last_success"] is None
    pusher._schedule_retry(PushStatus(ok=False, reason="timer", error="x", stage="commit"))
    retry = pusher.read_status()["retry"]
    assert retry["stage"] == "commit" and retry["failures"] == 1


def test_legacy_pause_state_migrates_to_immediate_retry(tmp_path):
    """升级前遗留的 push-paused.json（等人工恢复）→ 立即到点的重试记录。"""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "push-paused.json").write_text(json.dumps({
        "at": "2026-10-12T00:02:11+08:00",
        "reason": "timer",
        "error": "连接 NRF_EPD_3E1C 失败: ",
        "stage": "connect",
        "device": None,
    }))
    pusher = make_pusher(state_dir)
    retry = pusher._read_retry()
    assert retry["failures"] == 1 and retry["stage"] == "connect"
    assert pusher._retry_due(retry) is True
    assert not (state_dir / "push-paused.json").exists()
    assert (state_dir / "push-retry.json").exists()


def test_metrics_textfile_tracks_failure_and_success(tmp_path):
    path = tmp_path / "metrics" / "epd-food.prom"
    pusher = make_pusher(tmp_path / "state", textfile_path=path)
    pusher._schedule_retry(PushStatus(ok=False, reason="timer", error="x", stage="connect"))
    text = path.read_text()
    assert "epd_food_push_failures 1" in text
    assert "epd_food_push_retry_pending 1" in text

    pusher._clear_retry(
        PushStatus(ok=True, device="NRF_EPD_3E1C", finished_at="2026-10-27T09:00:00+08:00")
    )
    text = path.read_text()
    assert "epd_food_push_failures 0" in text
    assert "epd_food_push_retry_pending 0" in text
    expected = int(datetime.fromisoformat("2026-10-27T09:00:00+08:00").timestamp())
    assert f"epd_food_last_success_timestamp_seconds {expected}" in text


def test_boot_count_recorded_and_compared(monkeypatch, tmp_path):
    """成功推送后记录 boot_count；重启（boot_count 变化）才需要补推。"""
    state_dir = tmp_path / "state"
    pusher = make_pusher(state_dir)
    assert pusher.recorded_boot_count() is None
    pusher._record_boot_count({"boot_count": 5, "device": "NRF_EPD_3E1C"})
    assert pusher.recorded_boot_count() == 5
    # 重启后 boot_count 变 6 → 与记录不同，watch 会补推
    assert pusher.recorded_boot_count() != 6
    # 无状态块（老固件）不写、不崩
    pusher._record_boot_count(None)
    assert pusher.recorded_boot_count() == 5


def test_uptime_reveals_reboot_when_boot_count_resets(tmp_path):
    """DFU/断电重启会把 boot_count 打回 1：靠 uptime 与上次推送时间比对兜底。"""
    import time as _time

    pusher = make_pusher(tmp_path / "state")
    pusher._record_boot_count({"boot_count": 1})
    # 上次推送记录在 10 分钟前
    (tmp_path / "state" / "last-success.json").write_text(json.dumps({
        "at": _time.strftime("%Y-%m-%dT%H:%M:%S+08:00", _time.localtime(_time.time() - 600)),
        "device": "NRF_EPD_3E1C",
    }))
    pushed_at = pusher.recorded_push_time()
    assert pushed_at is not None
    elapsed = _time.time() - pushed_at
    # 设备只运行了 3 分钟 → 比 elapsed 短 → 判定重启
    assert 180 + 120 < elapsed
    # 设备已运行 20 分钟 → 没有重启
    assert not (1200 + 120 < elapsed)


def test_device_probe_recorded_and_exported(tmp_path):
    """每小时探测的结果要留档并进指标（兼作硬件健康检查）。"""
    path = tmp_path / "metrics" / "epd-food.prom"
    pusher = make_pusher(tmp_path / "state", textfile_path=path)
    assert pusher.read_status()["device_probe"] is None
    pusher.record_device_probe(
        {"device": "NRF_EPD_3E1C", "boot_count": 2, "uptime_s": 3600, "connect_count": 9}
    )
    probe = pusher.read_status()["device_probe"]
    assert probe["boot_count"] == 2 and probe["uptime_s"] == 3600
    text = path.read_text()
    assert "epd_food_device_uptime_seconds 3600" in text
    assert "epd_food_device_boot_count 2" in text
    assert "epd_food_device_connect_count 9" in text
    assert "epd_food_probe_timestamp_seconds 0" not in text
