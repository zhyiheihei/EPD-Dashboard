"""CalDAV 只读客户端与日程栏推送集成测试（HTTP 层 mock，不发真实请求）。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from epd_food_server import caldav, protocol
from epd_food_server.caldav import (
    CalDavClient,
    CalDavConfig,
    CalDavError,
    CalEvent,
    parse_ics,
)
from epd_food_server.pusher import Pusher

TZ = ZoneInfo("Asia/Shanghai")
WIN_START = datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)
WIN_END = WIN_START + timedelta(days=7)


def ics(dtstart: str, summary: str = "测试事件", **extra: str) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "BEGIN:VEVENT",
        f"DTSTART:{dtstart}",
        *[
            f"{key}:{value}" if not key.startswith("X-") else value
            for key, value in extra.items()
        ],
        f"SUMMARY:{summary}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


# ---------- ICS 解析 ----------

def test_parse_utc_event_in_window():
    events = parse_ics(ics("20260911T020000Z"), TZ, WIN_START, WIN_END)
    assert len(events) == 1
    assert events[0].summary == "测试事件"
    assert events[0].start_utc == datetime(2026, 9, 11, 2, 0, tzinfo=timezone.utc)


def test_parse_tzid_event():
    events = parse_ics(
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
        "DTSTART;TZID=Asia/Shanghai:20260911T140000\r\n"
        "SUMMARY:带时区\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        TZ,
        WIN_START,
        WIN_END,
    )
    assert events[0].start_utc.utcoffset() == timedelta(hours=8)
    assert events[0].start_utc.astimezone(timezone.utc).hour == 6


def test_parse_all_day_event():
    events = parse_ics(
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
        "DTSTART;VALUE=DATE:20260912\r\nSUMMARY:全天\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n",
        TZ,
        WIN_START,
        WIN_END,
    )
    assert events[0].start_utc == datetime(2026, 9, 12, 0, 0, tzinfo=TZ)


def test_parse_duration_and_escaped_summary():
    events = parse_ics(
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:20260911T020000Z\r\n"
        "DURATION:PT1H30M\r\nSUMMARY:a\\,b\\;c\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        TZ,
        WIN_START,
        WIN_END,
    )
    assert events[0].summary == "a,b;c"


def test_folded_line_unfolds():
    text = "BEGIN:VEVENT\r\nDTSTART:20260911T020000Z\r\nSUMMARY:很长的标题\r\n 第二行接续\r\nEND:VEVENT"
    events = parse_ics(text, TZ, WIN_START, WIN_END)
    assert events[0].summary == "很长的标题第二行接续"


def test_event_outside_window_excluded():
    events = parse_ics(ics("20261001T020000Z"), TZ, WIN_START, WIN_END)
    assert events == []


def test_daily_rrule_expands():
    text = (
        "BEGIN:VEVENT\r\nDTSTART:20260910T010000Z\r\nRRULE:FREQ=DAILY;COUNT=5\r\n"
        "SUMMARY:每日站会\r\nEND:VEVENT\r\n"
    )
    events = parse_ics(text, TZ, WIN_START, WIN_END)
    assert len(events) == 5
    assert [e.start_utc.day for e in events] == [10, 11, 12, 13, 14]


def test_weekly_rrule_excluded_instances_respect_window():
    text = (
        "BEGIN:VEVENT\r\nDTSTART:20260901T010000Z\r\nRRULE:FREQ=WEEKLY\r\n"
        "SUMMARY:周会\r\nEND:VEVENT\r\n"
    )
    events = parse_ics(text, TZ, WIN_START, WIN_END)
    # 9 月 1 日起每周二；窗口 9/10–9/17 内应命中 9/15 一个实例
    assert [e.start_utc for e in events] == [datetime(2026, 9, 15, 1, 0, tzinfo=timezone.utc)]


def test_unknown_freq_rrule_keeps_first_occurrence():
    text = (
        "BEGIN:VEVENT\r\nDTSTART:20260911T010000Z\r\nRRULE:FREQ=MONTHLY;BYDAY=2MO\r\n"
        "SUMMARY:月会\r\nEND:VEVENT\r\n"
    )
    events = parse_ics(text, TZ, WIN_START, WIN_END)
    assert [e.start_utc for e in events] == [datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc)]


def test_rdate_unsupported_fields_ignored():
    text = (
        "BEGIN:VEVENT\r\nDTSTART:20260911T010000Z\r\nATTENDEE:x@y.z\r\n"
        "DESCRIPTION:无关字段\r\nSUMMARY:只取需要字段\r\nEND:VEVENT\r\n"
    )
    assert len(parse_ics(text, TZ, WIN_START, WIN_END)) == 1


# ---------- multistatus / 客户端 ----------

MULTISTATUS = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response>
    <d:href>/dav/zhyi/calendar/abc.ics</d:href>
    <d:propstat><d:prop>
      <c:calendar-data>BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20260911T020000Z
SUMMARY:来自REPORT
END:VEVENT
END:VCALENDAR</c:calendar-data>
    </d:prop></d:propstat>
  </d:response>
</d:multistatus>"""

PROPFIND_OK = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response>
    <d:href>/dav/zhyi/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/zhyi/calendar/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype></d:prop></d:propstat>
  </d:response>
</d:multistatus>"""


def _client(calendar: str = "") -> CalDavClient:
    return CalDavClient(
        CalDavConfig(url="https://cal.example/dav/zhyi/", user="zhyi", password="pw", calendar=calendar)
    )


def test_fetch_events_parses_report_response():
    with patch.object(CalDavClient, "_request", return_value=MULTISTATUS) as mock:
        events = _client(calendar="calendar").fetch_events(WIN_START, WIN_END, TZ)
    assert len(events) == 1
    assert events[0].summary == "来自REPORT"
    # 未配置 calendar 时应先 PROPFIND 发现
    with patch.object(CalDavClient, "_request", return_value=PROPFIND_OK) as mock2:
        _client().fetch_events(WIN_START, WIN_END, TZ)
        assert mock2.call_args_list[0].args[0] == "PROPFIND"


def test_http_error_raises_caldav_error():
    with patch.object(
        CalDavClient,
        "_request",
        side_effect=CalDavError("CalDAV REPORT https://x -> HTTP 403"),
    ):
        try:
            _client(calendar="calendar").fetch_events(WIN_START, WIN_END, TZ)
        except CalDavError as exc:
            assert "403" in str(exc)
        else:
            raise AssertionError("应抛出 CalDavError")


# ---------- Pusher 日程集成 ----------

def make_schedule_pusher(events: list[CalEvent], fail: bool = False) -> Pusher:
    import tempfile

    pusher = object.__new__(Pusher)
    pusher._cfg = type(
        "Cfg",
        (),
        {
            "caldav_url": "https://cal.example/dav/zhyi/",
            "caldav_user": "zhyi",
            "caldav_password": "pw",
            "caldav_calendar": "calendar",
            "caldav_enabled": True,
            "schedule_days": 7,
            "timezone": "Asia/Shanghai",
            "state_dir": Path(tempfile.mkdtemp(prefix="epd-sched-")),
        },
    )()
    pusher._font = None

    def fake_fetch(self, start, end, tz):
        if fail:
            raise CalDavError("HTTP 403")
        return events

    # 每次调用临时替换类方法，调用后恢复
    def fetch_via_patcher():
        original = CalDavClient.fetch_events
        CalDavClient.fetch_events = fake_fetch
        try:
            return Pusher._fetch_schedules(pusher)
        finally:
            CalDavClient.fetch_events = original

    pusher._fetch_schedules = fetch_via_patcher
    return pusher


def test_pusher_schedules_fetch_and_render():
    # _fetch_schedules 只保留 start >= now 的事件，窗口必须锚定在当前时刻，
    # 否则用固定日期的测试会随真实时间流逝而失效（曾被 2026-09-11 炸过）
    events = [
        CalEvent(summary="牙医", start_utc=datetime.now(timezone.utc) + timedelta(days=1, hours=3))
    ]
    pusher = make_schedule_pusher(events)
    schedules, bitmaps = pusher._fetch_schedules()
    assert len(schedules) == 1
    assert schedules[0].slot == 0
    assert schedules[0].start_utc == int(events[0].start_utc.timestamp())
    # 320x20 = 800 字节，与协议推荐尺寸一致
    assert len(bitmaps[0]) == protocol.SCHEDULE_BITMAP_WIDTH // 8 * protocol.SCHEDULE_BITMAP_HEIGHT
    # 标题应带周几前缀
    assert "周五" in events[0].summary or bitmaps[0]


def test_pusher_degrades_on_caldav_failure():
    pusher = make_schedule_pusher([], fail=True)
    schedules, bitmaps = pusher._fetch_schedules()
    assert schedules == [] and bitmaps == []


def test_pusher_schedule_fingerprint_triggers_full_refresh():
    pusher = make_schedule_pusher(
        [CalEvent(summary="牙医", start_utc=datetime.now(timezone.utc) + timedelta(days=1))]
    )
    _, bitmaps_first = pusher._fetch_schedules()
    assert pusher._schedules_changed(bitmaps_first) is True  # 首次：无记录
    assert pusher._schedules_changed(bitmaps_first) is False  # 相同位图
    _, bitmaps_new = pusher._fetch_schedules()
    # 位图相同则指纹不变
    assert pusher._schedules_changed(bitmaps_new) is False


def test_schedule_slots_within_protocol_limits():
    now = datetime.now(timezone.utc)
    events = [
        CalEvent(summary=f"事件{i}", start_utc=now + timedelta(hours=i + 1))
        for i in range(5)
    ]
    pusher = make_schedule_pusher(events)
    schedules, bitmaps = pusher._fetch_schedules()
    assert len(schedules) <= protocol.MAX_SCHEDULES
    assert len(bitmaps) <= protocol.MAX_SCHEDULES
    assert [s.slot for s in schedules] == [0, 1]