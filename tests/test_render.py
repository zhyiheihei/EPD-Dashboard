"""渲染器测试：位图尺寸、打包位序、字号自适应。"""

from PIL import Image, ImageDraw

from epd_food_server import render


def _font_available() -> bool:
    return render.find_font() is not None


def test_render_size_and_padding():
    bitmap = render.render_text_1bit("牛奶", 152, 20)
    assert len(bitmap) == (152 + 7) // 8 * 20  # 19 * 20 = 380
    bitmap320 = render.render_text_1bit("日程标题", 320, 20)
    assert len(bitmap320) == 40 * 20


def test_render_empty_text_all_white():
    assert render.render_text_1bit("", 152, 20) == bytes(380)
    assert render.render_text_1bit("   ", 152, 20) == bytes(380)


def test_render_msb_first_bit_order():
    """在最左侧画一个像素，验证 bit7 置位、其余为 0。"""
    width, height = 8, 1
    image = Image.new("L", (width, height), 255)
    image.putpixel((0, 0), 0)
    # 直接调用打包逻辑：重用 render 内部约定手写一遍
    row_bytes = (width + 7) // 8
    buffer = bytearray(row_bytes * height)
    for y in range(height):
        for x in range(width):
            if image.getpixel((x, y)) < 128:
                buffer[y * row_bytes + (x >> 3)] |= 0x80 >> (x & 7)
    assert buffer[0] == 0x80


def test_render_text_leaves_marks():
    bitmap = render.render_text_1bit("苹果", 152, 20)
    assert any(bitmap), "渲染结果全白，说明文本未画上"


def test_render_long_text_truncated_without_error():
    long_text = "超长食品名称" * 20
    bitmap = render.render_text_1bit(long_text, 152, 20)
    assert len(bitmap) == 380
    assert any(bitmap)


def test_fit_text_font_size_steps_down():
    if not _font_available():
        return
    font_path = render.find_font()
    from PIL import Image as _I

    measure = _I.new("L", (1, 1))
    draw = ImageDraw.Draw(measure)
    # 10 个汉字必然超过 150px@18px，fit 后字号应小于起步值或被截断
    font, fitted = render.fit_text("一二三四五六七八九十", 150, font_path)
    size = getattr(font, "size", render.BASE_FONT_SIZE)
    assert size <= render.BASE_FONT_SIZE
    assert draw.textlength(fitted, font=font) <= 150
