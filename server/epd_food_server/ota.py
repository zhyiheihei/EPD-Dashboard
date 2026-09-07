"""OTA 包管理：nrfutil zip 解析、init 包 protobuf 提取版本、存储与升级编排。

nrfutil 6.x 生成的 OTA zip 内含 manifest.json 与 application 的 bin/dat；
dat 为 dfu-cc protobuf 编码的 init 包，其中 field3 子消息的 field3 即 application_version。
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import dfu
from .config import Config
from .db import Database

log = logging.getLogger(__name__)

MAX_ZIP_SIZE = 20 * 1024 * 1024


class OtaPackageError(ValueError):
    """OTA 包无效。"""


# ---------- protobuf 最小 wire 解析（dfu-cc InitPacket） ----------

def _read_varint(data: bytes, index: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while index < len(data):
        byte = data[index]
        result |= (byte & 0x7F) << shift
        index += 1
        if not byte & 0x80:
            return result, index
        shift += 7
        if shift > 63:
            break
    raise OtaPackageError("init 包 varint 解析越界")


def _protobuf_fields(data: bytes):
    """逐字段产出 (field_no, wire_type, value)。"""
    index = 0
    while index < len(data):
        key, index = _read_varint(data, index)
        field, wire = key >> 3, key & 0x07
        if wire == 0:
            value, index = _read_varint(data, index)
        elif wire == 1:
            value, index = data[index : index + 8], index + 8
        elif wire == 2:
            length, index = _read_varint(data, index)
            value, index = data[index : index + length], index + length
        elif wire == 5:
            value, index = data[index : index + 4], index + 4
        else:
            raise OtaPackageError(f"不支持的 protobuf wire type {wire}")
        yield field, wire, value


def _try_parse_fields(data: bytes):
    try:
        return list(_protobuf_fields(data))
    except OtaPackageError:
        return None


def parse_init_packet(dat: bytes, bin_size: int | None = None) -> dict:
    """从 init 包提取版本元信息（对真实 nrfutil 产物逆向校验过的结构）。

    包络为 dfu-cc protobuf；版本元数据消息（含固件大小 varint == bin_size）内：
    field1 = application_version，field2 = hw_version，field3 = sd_req（packed uint16 LE）。
    """
    if bin_size is None:
        return {}

    def visit(fields):
        for _field, wire, value in fields:
            if wire != 2:
                continue
            sub = _try_parse_fields(value)
            if sub is None:
                continue
            if any(w == 0 and v == bin_size for _f, w, v in sub):
                return sub
            found = visit(sub)
            if found is not None:
                return found
        return None

    try:
        root = list(_protobuf_fields(dat))
    except OtaPackageError:
        return {}
    meta = visit(root)
    if meta is None:
        return {}

    info: dict = {}
    for field, wire, value in meta:
        if wire != 0:
            continue
        if value == bin_size:
            info["firmware_size"] = value
        elif field == 1:
            info["application_version"] = value
        elif field == 2:
            info["hw_version"] = value
    for field, wire, value in meta:
        if wire == 2 and field == 3 and value:  # sd_req: packed repeated uint32（varint 序列）
            values = []
            index = 0
            try:
                while index < len(value):
                    sd, index = _read_varint(value, index)
                    values.append(sd)
            except OtaPackageError:
                pass
            if values:
                info["sd_required"] = ",".join(f"0x{x:X}" for x in values)
    return info


def parse_app_version_from_filename(filename: str) -> int | None:
    """EPD-nRF5 构建约定：*-v<HEX>-ota.zip，HEX 即 APP_VERSION（如 v1F → 31）。"""
    import re

    match = re.search(r"-v([0-9A-Fa-f]{1,3})-ota\.zip$", filename)
    return int(match.group(1), 16) if match else None


def parse_ota_zip(path: str | Path) -> dict:
    """解析 nrfutil OTA 包，返回 {bin, dat, app_version, device_type, sd_required, manifest}。"""
    path = Path(path)
    if path.stat().st_size > MAX_ZIP_SIZE:
        raise OtaPackageError(f"OTA 包超过 {MAX_ZIP_SIZE // (1024 * 1024)}MB 上限")
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if "manifest.json" not in names:
                raise OtaPackageError("缺少 manifest.json，不是 nrfutil OTA 包")
            manifest = json.loads(archive.read("manifest.json"))
            application = (manifest.get("manifest") or {}).get("application")
            if not application:
                raise OtaPackageError("manifest 中缺少 application 条目")
            bin_data = archive.read(application["bin_file"]) if application.get("bin_file") else b""
            dat_data = archive.read(application["dat_file"]) if application.get("dat_file") else b""
    except OtaPackageError:
        raise
    except KeyError as exc:
        raise OtaPackageError(f"manifest 引用的文件缺失: {exc}") from exc
    except Exception as exc:
        raise OtaPackageError(f"无法读取 OTA 包: {exc}") from exc

    if not bin_data:
        raise OtaPackageError("OTA 包中没有应用固件")

    init_meta = parse_init_packet(dat_data, bin_size=len(bin_data)) if dat_data else {}
    if "application_version" not in init_meta:
        init_meta["application_version"] = parse_app_version_from_filename(path.name)
    init_meta["bin"] = bin_data
    init_meta["dat"] = dat_data
    init_meta["manifest"] = manifest
    return init_meta


# ---------- 存储 ----------

@dataclass
class OtaPackageRow:
    id: int
    filename: str
    app_version: int | None
    device_type: int | None
    softdevice_required: str | None
    size: int
    sha256: str
    stored_path: str
    uploaded_at: str

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class OtaStore:
    """OTA 包落盘 + firmware_packages 表。"""

    def __init__(self, cfg: Config, db: Database):
        self._cfg = cfg
        self._db = db
        self._dir = cfg.state_dir / "ota"
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def status_path(self) -> Path:
        return self._cfg.state_dir / "ota-status.json"

    @property
    def lock_path(self) -> Path:
        return self._cfg.state_dir / "ota.lock"

    def save_from_upload(self, upload_name: str, content: bytes) -> dict:
        """校验并保存上传的 OTA 包，返回 DB 行（同 sha256 幂等返回已有行）。"""
        if len(content) > MAX_ZIP_SIZE:
            raise OtaPackageError(f"OTA 包超过 {MAX_ZIP_SIZE // (1024 * 1024)}MB 上限")
        tmp_path = self._dir / f"upload-{os.getpid()}.tmp"
        tmp_path.write_bytes(content)
        try:
            parsed = parse_ota_zip(tmp_path)
        except OtaPackageError:
            tmp_path.unlink(missing_ok=True)
            raise
        sha = hashlib.sha256(content).hexdigest()
        existing = self._db.get_firmware_by_sha(sha)
        safe_name = Path(upload_name or "firmware.zip").name
        if existing is not None:
            tmp_path.unlink(missing_ok=True)
            return existing
        filename = f"{sha[:12]}-{safe_name}"
        stored = self._dir / filename
        os.replace(tmp_path, stored)
        return self._db.insert_firmware(
            filename=safe_name,
            app_version=parsed.get("application_version"),
            device_type=parsed.get("hw_version"),
            softdevice_required=parsed.get("sd_required"),
            size=len(content),
            sha256=sha,
            stored_path=str(stored),
        )

    def load_payload(self, row: dict) -> dict:
        parsed = parse_ota_zip(row["stored_path"])
        return parsed

    def read_status(self) -> dict:
        status: dict = {}
        try:
            status = json.loads(self.status_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("OTA 状态文件读取失败: %s", exc)
        return status


# ---------- 升级编排 ----------

class OtaRunner:
    def __init__(self, cfg: Config, store: OtaStore):
        self._cfg = cfg
        self._store = store
        self._busy = asyncio.Lock()
        self._in_progress = False

    @property
    def in_progress(self) -> bool:
        return self._in_progress

    def read_status(self) -> dict:
        status = self._store.read_status()
        status["in_progress"] = self._in_progress
        return status

    async def upgrade(self, row: dict) -> dict:
        if self._busy.locked():
            return {"started": False, "busy": True}
        async with self._busy:
            return await asyncio.to_thread(self._upgrade_blocking, row)

    def _upgrade_blocking(self, row: dict) -> dict:
        self._in_progress = True
        started = time.monotonic()
        status: dict = {
            "ok": None,
            "firmware": row.get("filename"),
            "firmware_id": row.get("id"),
            "app_version": row.get("app_version"),
            "device": None,
            "sent_bytes": 0,
            "total_bytes": None,
            "started_at": _iso_now(),
            "finished_at": None,
            "duration_s": None,
            "error": None,
        }
        lock_fd = self._acquire_lock()
        if lock_fd is None:
            status["error"] = "等待 OTA 锁超时"
            status["finished_at"] = _iso_now()
            self._write_status(status)
            self._in_progress = False
            return status
        try:
            payload = self._store.load_payload(row)
            status["total_bytes"] = len(payload["bin"]) + len(payload["dat"])

            def on_progress(sent: int, total: int) -> None:
                status["sent_bytes"] = sent
                status["total_bytes"] = total
                self._write_status(status)

            attempts = 1 + max(0, self._cfg.push_retries)
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    status["device"] = asyncio.run(
                        dfu.upgrade_on_device(self._cfg, payload["dat"], payload["bin"], on_progress)
                    )
                    status["ok"] = True
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001 — 单次失败重试
                    last_error = exc
                    status["error"] = str(exc) or exc.__class__.__name__
                    log.warning("第 %s/%s 次 OTA 失败: %s", attempt, attempts, exc)
                    if attempt < attempts:
                        time.sleep(3.0 * attempt)
            if last_error is not None:
                status["ok"] = False
                status["error"] = str(last_error) or last_error.__class__.__name__
        except Exception as exc:
            log.exception("OTA 升级失败")
            status["ok"] = False
            status["error"] = str(exc)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            status["duration_s"] = round(time.monotonic() - started, 1)
            status["finished_at"] = _iso_now()
            self._in_progress = False
            self._write_status(status)
        return status

    def _acquire_lock(self):
        self._cfg.state_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 90.0
        while True:
            fd = os.open(self._store.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                os.close(fd)
                if time.monotonic() >= deadline:
                    return None
                time.sleep(1.0)

    def _write_status(self, status: dict) -> None:
        try:
            self._cfg.state_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self._store.status_path, status)
        except Exception as exc:
            log.warning("OTA 状态文件写入失败: %s", exc)
