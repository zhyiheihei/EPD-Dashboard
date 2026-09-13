"""push-history.jsonl 历史落盘与滚动保留。"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "server")

from epd_food_server.pusher import PUSH_HISTORY_KEEP, Pusher, PushStatus


def make_pusher(state_dir: Path) -> Pusher:
    pusher = object.__new__(Pusher)
    pusher._cfg = type("Cfg", (), {"state_dir": state_dir})()
    return pusher


def test_history_appended_and_trimmed():
    with tempfile.TemporaryDirectory() as tmp:
        pusher = make_pusher(Path(tmp))
        for i in range(PUSH_HISTORY_KEEP + 5):
            pusher._append_history(
                PushStatus(ok=True, reason="timer", duration_s=float(i), started_at=f"t{i}")
            )
        lines = (Path(tmp) / "push-history.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == PUSH_HISTORY_KEEP
        # 保留的是最近 PUSH_HISTORY_KEEP 条，且每行是合法 JSON
        first = json.loads(lines[0])
        last = json.loads(lines[-1])
        assert first["duration_s"] == float(5)
        assert last["duration_s"] == float(PUSH_HISTORY_KEEP + 4)
        assert last["ok"] is True


def test_history_write_failure_does_not_raise():
    with tempfile.TemporaryDirectory() as tmp:
        pusher = make_pusher(Path(tmp) / "state")
        pusher._append_history(PushStatus(ok=True))  # state_dir 不存在也应自动建目录
        assert (Path(tmp) / "state" / "push-history.jsonl").exists()
