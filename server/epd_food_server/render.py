"""文本 → 1-bit 位图渲染。

设备无法存储字库、也不接收文本编码：食品名称必须在服务端渲染成 1-bit 位图
（每行按字节补齐、左侧像素为 bit7、1=黑），随 BLE 看板协议 v1 下发。
渲染风格与固件网页参考实现（html/js/main.js renderTextBitmap）对齐：
18px 起步、垂直居中、超宽自动缩小字号、仍放不下则省略号截断。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# NixOS/常见发行版的 CJK 字体路径，按序探测
FONT_CANDIDATES = [
    "/run/current-system/sw/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/run/current-system/sw/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/run/current-system/sw/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]

BASE_FONT_SIZE = 18
MIN_FONT_SIZE = 12
ELLIPSIS = "…"

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_ttc_index_cache: dict[str, int] = {}


def _fontconfig_lookup() -> str | None:
    """通过 fontconfig 找 CJK 粗体（NixOS 上 systemPackages 字体也能被找到）。"""
    fc_match = shutil.which("fc-match")
    if fc_match is None:
        return None
    try:
        result = subprocess.run(
            [fc_match, "-f", "%{file}", "Noto Sans CJK SC:style=Bold"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        path = result.stdout.strip()
        return path if path and Path(path).is_file() else None
    except Exception:
        return None


def find_font(configured: str | None = None) -> str | None:
    """返回可用的 CJK 字体文件路径；找不到返回 None（退回 PIL 内置字体，中文会变方块）。"""
    if configured:
        path = Path(configured)
        if path.is_file():
            return str(path)
        log.warning("配置的字体不存在: %s，回退自动探测", configured)
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    via_fc = _fontconfig_lookup()
    if via_fc:
        return via_fc
    log.warning("未找到中文字体，请安装 noto-fonts-cjk-sans 或设置 EPD_FOOD_FONT_PATH")
    return None


def _pick_ttc_index(path: str, size: int, prefer: str = "SC") -> int:
    """ttc 字体集合里优先选择简体中文（SC）子字体。"""
    if not path.lower().endswith((".ttc", ".otc")):
        return 0
    if path in _ttc_index_cache:
        return _ttc_index_cache[path]
    chosen = 0
    for index in range(8):
        try:
            family, style = ImageFont.truetype(path, size, index=index).getname()
        except Exception:
            break
        if prefer in f"{family} {style}":
            chosen = index
            break
    _ttc_index_cache[path] = chosen
    return chosen


def _apply_bold(font: ImageFont.FreeTypeFont) -> ImageFont.FreeTypeFont:
    """可变字体默认实例通常是 Regular，小字号上屏后笔画太细，强制设为 Bold（wght 700）。"""
    try:
        axes = font.get_variation_axes()
    except Exception:
        return font
    values = []
    for axis in axes:
        name = axis.get("name") if isinstance(axis, dict) else axis.name
        if isinstance(name, bytes):
            name = name.decode(errors="ignore")
        if str(name).lower() in ("wght", "weight"):
            values.append(700)
        else:
            values.append(axis.get("default") if isinstance(axis, dict) else axis.default)
    try:
        font.set_variation_by_axes(values)
    except Exception:
        pass
    return font


def load_font(path: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if not path:
        return ImageFont.load_default()
    key = (path, size)
    if key not in _font_cache:
        font = ImageFont.truetype(path, size, index=_pick_ttc_index(path, size))
        if isinstance(font, ImageFont.FreeTypeFont):
            font = _apply_bold(font)
        _font_cache[key] = font
    return _font_cache[key]


def fit_text(
    text: str,
    max_width: int,
    font_path: str | None,
) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, str]:
    """18px 起步逐步缩小到 12px；仍超宽则截断并追加省略号。"""
    measure = Image.new("L", (1, 1))
    draw = ImageDraw.Draw(measure)
    size = BASE_FONT_SIZE
    while size >= MIN_FONT_SIZE:
        font = load_font(font_path, size)
        if draw.textlength(text, font=font) <= max_width:
            return font, text
        size -= 1
    font = load_font(font_path, MIN_FONT_SIZE)
    while text:
        candidate = text + ELLIPSIS
        if draw.textlength(candidate, font=font) <= max_width:
            return font, candidate
        text = text[:-1]
    return font, ELLIPSIS


def render_text_1bit(
    text: str,
    width: int = 152,
    height: int = 20,
    font_path: str | None = None,
) -> bytes:
    """渲染为 1-bit 打包位图（MSB-first、行按字节补齐、1=黑、0=白）。"""
    row_bytes = (width + 7) // 8
    buffer = bytearray(row_bytes * height)
    if not text:
        return bytes(buffer)

    resolved = font_path if font_path is not None else find_font()
    font, fitted = fit_text(text, width - 2, resolved)

    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    draw.text((1, height // 2), fitted, font=font, fill=0, anchor="lm")

    pixels = image.load()
    for y in range(height):
        row_base = y * row_bytes
        for x in range(width):
            if pixels[x, y] < 128:
                buffer[row_base + (x >> 3)] |= 0x80 >> (x & 7)
    return bytes(buffer)
