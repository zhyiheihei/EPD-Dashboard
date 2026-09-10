"""只读 CalDAV 客户端（RFC 4791 / RFC 5545），为看板日程栏拉取近期事件。

按标准协议实现，不对具体服务端（Radicale 等）做专项适配：

1. PROPFIND（Depth 1）发现日历集合——配置了日历路径则跳过，直接使用
2. REPORT calendar-query（time-range 过滤）拉取窗口内 VEVENT 及其 calendar-data
3. 极简 RFC 5545 ICS 解析：DTSTART/DTEND/DURATION/SUMMARY，支持
   UTC Z、TZID、VALUE=DATE 三种时间格式，以及 DAILY/WEEKLY RRULE 基础展开

只用标准库（urllib + xml.etree），不引入运行时依赖。
"""

from __future__ import annotations

import base64
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
DAV_NS = "DAV:"

# RRULE 展开安全上限：防 COUNT=99999 之类的病态规则拖死推送
_MAX_INSTANCES_PER_EVENT = 64

_DATE_RE = re.compile(r"\d{8}")
_DURATION_RE = re.compile(
    r"([+-]?)P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
    re.IGNORECASE,
)


class CalDavError(RuntimeError):
    """CalDAV 请求层错误（网络 / HTTP 状态 / 响应解析）。"""


@dataclass(frozen=True)
class CalEvent:
    summary: str
    start_utc: datetime  # 已转为带 tzinfo 的绝对时间


@dataclass(frozen=True)
class CalDavConfig:
    url: str  # 日历集合 URL，或其上级（配合 calendar 自动发现）
    user: str = ""
    password: str = ""
    calendar: str = ""  # 日历集合路径；空 = PROPFIND 自动发现
    days: int = 7


# ---------- ICS 解析 ----------

def _unfold_lines(text: str) -> list[str]:
    """RFC 5545 折行还原：以空格/制表符开头的行拼回上一行。"""
    out: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _parse_prop(line: str) -> tuple[str, dict[str, str], str]:
    """`NAME;PARAM=V;PARAM2=V2:value` → (NAME, params, value)。"""
    colon = line.find(":")
    if colon < 0:
        return line.upper(), {}, ""
    head, value = line[:colon], line[colon + 1 :]
    parts = head.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for param in parts[1:]:
        if "=" in param:
            key, _, val = param.partition("=")
            params[key.upper()] = val.strip('"')
    return name, params, value


def _parse_ics_dt(value: str, params: dict[str, str], default_tz: ZoneInfo) -> datetime | None:
    """DTSTART/DTEND 值：UTC Z / TZID 本地时间 / 全天日期三种格式。"""
    value = value.strip()
    tz = ZoneInfo(params["TZID"]) if params.get("TZID") else default_tz
    if params.get("VALUE") == "DATE" or _DATE_RE.fullmatch(value):
        # 全天事件：按日历本地时区的零点处理
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=tz)
    if value.endswith("Z"):
        return datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo("UTC"))
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=tz)


def _parse_duration(value: str) -> timedelta | None:
    """RFC 5545 DURATION：P[n]W[n]D / PT[n]H[n]M[n]S（含 ± 前缀）。"""
    match = _DURATION_RE.fullmatch(value.strip())
    if not match:
        return None
    sign, weeks, days, hours, minutes, seconds = match.groups()
    delta = timedelta(
        weeks=int(weeks or 0),
        days=int(days or 0),
        hours=int(hours or 0),
        minutes=int(minutes or 0),
        seconds=int(seconds or 0),
    )
    return -delta if sign == "-" else delta


def _expand_rrule(
    rrule: str, start: datetime, duration: timedelta, horizon_utc: datetime
) -> list[datetime]:
    """DAILY/WEEKLY 基础展开（INTERVAL/COUNT/UNTIL）；其他频率按单次处理。"""
    props = {}
    for part in rrule.strip().split(";"):
        if "=" in part:
            key, _, val = part.partition("=")
            props[key.upper()] = val
    freq = props.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY"):
        # MONTHLY/YEARLY/BYDAY 等复杂规则不展开，仅保留首例
        return [start]
    interval = max(1, int(props.get("INTERVAL") or 1))
    step = timedelta(days=interval) if freq == "DAILY" else timedelta(weeks=interval)
    count = int(props["COUNT"]) if "COUNT" in props else None
    until: datetime | None = None
    if "UNTIL" in props:
        raw = props["UNTIL"].strip()
        try:
            if raw.endswith("Z"):
                until = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
            elif _DATE_RE.fullmatch(raw):
                until = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=start.tzinfo)
            else:
                until = datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=start.tzinfo)
        except ValueError:
            return [start]
    out: list[datetime] = []
    occurrence = start
    while len(out) < _MAX_INSTANCES_PER_EVENT:
        if occurrence > horizon_utc:
            break
        if until is not None and occurrence > until:
            break
        out.append(occurrence)
        if count is not None and len(out) >= count:
            break
        occurrence += step
    return out


def _build_event(
    block: dict[str, tuple[dict[str, str], str]],
    default_tz: ZoneInfo,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> list[CalEvent]:
    """从单个 VEVENT 属性块生成窗口内有交集的事件实例列表。"""
    if "DTSTART" not in block:
        return []
    start_params, start_value = block["DTSTART"]
    all_day = start_params.get("VALUE") == "DATE" or bool(_DATE_RE.fullmatch(start_value.strip()))
    try:
        start = _parse_ics_dt(start_value, start_params, default_tz)
    except Exception:
        log.warning("ICS DTSTART 解析失败: %s", start_value)
        return []
    if start is None:
        return []

    duration: timedelta | None = None
    if "DTEND" in block:
        try:
            end = _parse_ics_dt(*block["DTEND"], default_tz)
            if end:
                duration = end - start
        except Exception:
            pass
    if duration is None and "DURATION" in block:
        duration = _parse_duration(block["DURATION"][1])
    if duration is None or duration <= timedelta(0):
        # 全天事件缺省 1 天；定时事件给 2 小时兜底（仅用于窗口求交）
        duration = timedelta(days=1) if all_day else timedelta(hours=2)

    _, rrule_value = block.get("RRULE", ({}, ""))
    starts = _expand_rrule(rrule_value, start, duration, window_end_utc) if rrule_value else [start]
    summary = (
        block.get("SUMMARY", ({}, ""))[1]
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\n", " ")
        .replace("\\N", " ")
    )
    return [
        CalEvent(summary=summary, start_utc=instance)
        for instance in starts
        if instance + duration >= window_start_utc and instance <= window_end_utc
    ]


def parse_ics(
    text: str,
    default_tz: ZoneInfo,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> list[CalEvent]:
    """解析 ICS 文本，返回与 [window_start, window_end] 有交集的事件（按开始时间升序）。"""
    events: list[CalEvent] = []
    block: dict[str, tuple[dict[str, str], str]] = {}
    in_event = False
    for line in _unfold_lines(text):
        stripped = line.strip()
        upper = stripped.upper()
        if upper == "BEGIN:VEVENT":
            in_event, block = True, {}
            continue
        if upper == "END:VEVENT":
            in_event = False
            events.extend(_build_event(block, default_tz, window_start_utc, window_end_utc))
            block = {}
            continue
        if in_event:
            name, params, value = _parse_prop(stripped)
            if name in ("DTSTART", "DTEND", "DURATION", "RRULE", "SUMMARY"):
                block[name] = (params, value)
    events.sort(key=lambda e: e.start_utc)
    return events


# ---------- CalDAV 客户端 ----------

class CalDavClient:
    """标准 CalDAV 只读访问：PROPFIND 发现 + REPORT calendar-query。"""

    def __init__(self, config: CalDavConfig, timeout: float = 15.0):
        self._config = config
        self._timeout = timeout
        auth = None
        if config.user:
            cred = f"{config.user}:{config.password}"
            auth = "Basic " + base64.b64encode(cred.encode()).decode()
        self._auth_header = auth

    def _request(self, method: str, url: str, body: str, depth: str) -> str:
        request = urllib.request.Request(url, data=body.encode("utf-8"), method=method)
        request.add_header("Content-Type", "application/xml; charset=utf-8")
        request.add_header("Depth", depth)
        if self._auth_header:
            request.add_header("Authorization", self._auth_header)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise CalDavError(f"CalDAV {method} {url} -> HTTP {exc.code}") from exc
        except Exception as exc:
            raise CalDavError(f"CalDAV {method} {url} 失败: {exc}") from exc

    def _calendar_url(self) -> str:
        base = self._config.url.rstrip("/") + "/"
        calendar = self._config.calendar
        if calendar:
            if calendar.startswith("http"):
                return calendar
            return urllib.parse.urljoin(base, calendar.strip("/") + "/")
        return base

    def discover_calendar(self) -> str:
        """PROPFIND Depth 1 找第一个日历集合（resourcetype 含 calendar）。"""
        url = self._config.url.rstrip("/") + "/"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<d:propfind xmlns:d="{DAV_NS}" xmlns:c="{CALDAV_NS}">'
            "<d:prop><d:resourcetype/></d:prop></d:propfind>"
        )
        for response in _parse_multistatus(self._request("PROPFIND", url, body, "1")):
            resource_type = response.find(f".//{{{DAV_NS}}}resourcetype")
            if resource_type is None:
                continue
            if resource_type.find(f"{{{CALDAV_NS}}}calendar") is not None:
                href = (response.findtext(f"{{{DAV_NS}}}href") or "").strip()
                if href:
                    return urllib.parse.urljoin(url, href)
        raise CalDavError(f"未在 {url} 下发现日历集合")

    def fetch_events(
        self, window_start_utc: datetime, window_end_utc: datetime, default_tz: ZoneInfo
    ) -> list[CalEvent]:
        """REPORT calendar-query 拉 time-range 内事件并解析 ICS。"""
        calendar_url = (
            self.discover_calendar() if not self._config.calendar else self._calendar_url()
        )
        start_text = window_start_utc.strftime("%Y%m%dT%H%M%SZ")
        end_text = window_end_utc.strftime("%Y%m%dT%H%M%SZ")
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<c:calendar-query xmlns:d="{DAV_NS}" xmlns:c="{CALDAV_NS}">'
            "<d:prop><c:calendar-data/></d:prop>"
            "<c:filter><c:comp-filter name=\"VCALENDAR\">"
            "<c:comp-filter name=\"VEVENT\">"
            f"<c:time-range start=\"{start_text}\" end=\"{end_text}\"/>"
            "</c:comp-filter>"
            "</c:comp-filter></c:filter></c:calendar-query>"
        )
        events: list[CalEvent] = []
        for response in _parse_multistatus(self._request("REPORT", calendar_url, body, "1")):
            data = response.find(f".//{{{CALDAV_NS}}}calendar-data")
            if data is None or not (data.text or "").strip():
                continue
            try:
                events.extend(parse_ics(data.text, default_tz, window_start_utc, window_end_utc))
            except Exception as exc:
                log.warning("ICS 解析失败（跳过该资源）: %s", exc)
        events.sort(key=lambda e: e.start_utc)
        return events


def _parse_multistatus(xml_text: str) -> list[ET.Element]:
    """解析 207 multistatus，返回每个 <d:response> 元素。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise CalDavError(f"CalDAV 响应 XML 解析失败: {exc}") from exc
    if root.tag != f"{{{DAV_NS}}}multistatus":
        raise CalDavError(f"意外的 CalDAV 响应根元素: {root.tag}")
    return root.findall(f"{{{DAV_NS}}}response")