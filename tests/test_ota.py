"""OTA 包解析测试：合成包（按真实 nrfutil 结构）+ EPD-nRF5 真实编译产物（存在时）。"""

import struct
import zipfile
from pathlib import Path

import pytest

from epd_food_server.ota import OtaPackageError, parse_init_packet, parse_ota_zip

_OTA_DIR = Path("/home/zhyi/Documents/repositories/EPD-nRF5/build/nrf52")
_OTA_ZIPS = sorted(_OTA_DIR.glob("*-ota.zip")) if _OTA_DIR.exists() else []
REAL_OTA_ZIP = _OTA_ZIPS[-1] if _OTA_ZIPS else Path("/nonexistent")


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _make_init_packet(application_version: int, hw_version: int, sd_required: int, bin_size: int) -> bytes:
    """按真实 nrfutil 产物逆向的结构构造 init 包（dfu-cc.proto InitCommand）：
    Packet{ field2: SignedCommand{ field1: Command(InitCommand) } }，
    InitCommand 内 field1=fw_version(app), field2=hw_version, field3=sd_req(packed varint),
    field7=app_size。
    """
    sd_packed = _varint(sd_required)  # packed repeated uint32 = varint 序列
    meta = bytes([1 << 3 | 0]) + _varint(application_version)
    meta += bytes([2 << 3 | 0]) + _varint(hw_version)
    meta += bytes([3 << 3 | 2]) + _varint(len(sd_packed)) + sd_packed
    meta += bytes([7 << 3 | 0]) + _varint(bin_size)
    signed = bytes([1 << 3 | 2]) + _varint(len(meta)) + meta + bytes([2 << 3 | 0]) + _varint(0)
    return bytes([2 << 3 | 2]) + _varint(len(signed)) + signed


def test_parse_init_packet_real_layout():
    bin_size = 56944
    dat = _make_init_packet(31, 52, 0x126, bin_size)
    meta = parse_init_packet(dat, bin_size=bin_size)
    assert meta["application_version"] == 31
    assert meta["hw_version"] == 52
    assert meta["sd_required"] == "0x126"
    assert meta["firmware_size"] == bin_size


def test_parse_init_packet_garbage_returns_empty():
    assert parse_init_packet(b"\xff\xff\xff\xff\xff", bin_size=100) == {}
    assert parse_init_packet(b"", bin_size=100) == {}


def test_synthetic_zip_roundtrip(tmp_path):
    """合成 nrfutil 结构的 OTA 包 → 解析出 bin/dat/版本/固件大小。"""
    import json

    bin_data = bytes((i * 13) % 256 for i in range(1024))
    dat_data = _make_init_packet(31, 52, 0x126, len(bin_data))
    zip_path = tmp_path / "test-v1F-ota.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"manifest": {"application": {"bin_file": "app.bin", "dat_file": "app.dat"}}}),
        )
        zf.writestr("app.bin", bin_data)
        zf.writestr("app.dat", dat_data)

    parsed = parse_ota_zip(zip_path)
    assert parsed["bin"] == bin_data
    assert parsed["dat"] == dat_data
    assert parsed["application_version"] == 31
    assert parsed["sd_required"] == "0x126"


def test_invalid_zip_rejected(tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("readme.txt", "not a dfu package")
    with pytest.raises(OtaPackageError, match="manifest"):
        parse_ota_zip(bad)


def test_non_zip_rejected(tmp_path):
    fake = tmp_path / "fake.zip"
    fake.write_bytes(b"this is not a zip at all" * 10)
    with pytest.raises(OtaPackageError):
        parse_ota_zip(fake)


def test_filename_version_fallback(tmp_path):
    """init 包不含版本时回退到文件名约定（*-v1F-ota.zip）。"""
    import json

    bin_data = bytes(512)
    zip_path = tmp_path / "EPD-nRF52-uc8179-v1F-ota.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"manifest": {"application": {"bin_file": "app.bin", "dat_file": "app.dat"}}}),
        )
        zf.writestr("app.bin", bin_data)
        zf.writestr("app.dat", b"\x00")  # 无有效 init 包结构
    parsed = parse_ota_zip(zip_path)
    assert parsed["application_version"] == 0x1F


@pytest.mark.skipif(not REAL_OTA_ZIP.exists(), reason="EPD-nRF5 OTA 产物未编译")
def test_real_firmware_package():
    """真实编译产物：init 包版本须与文件名版本约定（*-vXX-ota.zip）一致。"""
    parsed = parse_ota_zip(REAL_OTA_ZIP)
    from epd_food_server.ota import parse_app_version_from_filename

    expected = parse_app_version_from_filename(REAL_OTA_ZIP.name)
    assert expected is not None
    assert parsed["application_version"] == expected
    assert parsed["hw_version"] == 52
    assert parsed["sd_required"] == "0x126"  # S112 7.3.0
    assert parsed["firmware_size"] == len(parsed["bin"])
    assert len(parsed["bin"]) > 20 * 1024  # 应用固件几十 KB
    assert len(parsed["dat"]) > 100  # 签名过的 init 包
