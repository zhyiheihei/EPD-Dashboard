import sys
from pathlib import Path

# tests/ 位于仓库根，服务端包在 server/ 下
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))


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


def make_init_packet(application_version: int, hw_version: int, sd_required: int, bin_size: int) -> bytes:
    """按真实 nrfutil 产物逆向的结构构造 init 包（dfu-cc.proto InitCommand）。"""
    sd_packed = _varint(sd_required)
    meta = bytes([1 << 3 | 0]) + _varint(application_version)
    meta += bytes([2 << 3 | 0]) + _varint(hw_version)
    meta += bytes([3 << 3 | 2]) + _varint(len(sd_packed)) + sd_packed
    meta += bytes([7 << 3 | 0]) + _varint(bin_size)
    signed = bytes([1 << 3 | 2]) + _varint(len(meta)) + meta + bytes([2 << 3 | 0]) + _varint(0)
    return bytes([2 << 3 | 2]) + _varint(len(signed)) + signed


def make_ota_zip_bytes(app_version: int = 31, bin_size: int = 2048) -> bytes:
    """内存构造 nrfutil 结构的 OTA zip。"""
    import io
    import json
    import zipfile

    from epd_food_server.ota import parse_init_packet  # noqa: F401 — 确保导入可用

    bin_data = bytes((i * 13) % 256 for i in range(bin_size))
    dat_data = make_init_packet(app_version, 52, 0x126, bin_size)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"manifest": {"application": {"bin_file": "app.bin", "dat_file": "app.dat"}}}),
        )
        zf.writestr("app.bin", bin_data)
        zf.writestr("app.dat", dat_data)
    return buf.getvalue()
