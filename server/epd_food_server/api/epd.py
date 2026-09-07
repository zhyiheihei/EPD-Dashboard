"""推送控制 API：手动推送、状态查询、上屏预览。"""

from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response as HTTPResponse

from ..config import DRINK_CATEGORY
from ..models import PushPreviewItem
from ..render import find_font, render_text_1bit
from ..protocol import FOOD_BITMAP_HEIGHT, FOOD_BITMAP_WIDTH


def create_router(auth: Callable) -> APIRouter:
    router = APIRouter(dependencies=[Depends(auth)])

    @router.post("/epd/push", tags=["epd"])
    async def trigger_push(request: Request):
        """手动触发一次推送；立即返回，结果经 /api/epd/status 轮询。"""
        pusher = request.app.state.pusher
        if pusher.in_progress:
            return {"started": False, "busy": True}
        asyncio.create_task(pusher.push(reason="manual"))
        return {"started": True, "busy": False}

    @router.get("/epd/status", tags=["epd"])
    def push_status(request: Request):
        pusher = request.app.state.pusher
        return pusher.read_status()

    @router.get("/epd/preview", response_model=list[PushPreviewItem], tags=["epd"])
    def push_preview(request: Request):
        """当前将上屏的 Top4 及名称位图可渲染性，供客户端做墨水屏效果预览。"""
        cfg = request.app.state.cfg
        db = request.app.state.db
        font = find_font(cfg.font_path)
        if font is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "未找到可用中文字体，无法渲染位图（检查 EPD_FOOD_FONT_PATH）",
            )
        items = []
        for index, row in enumerate(db.top_for_display(limit=4)):
            bitmap = render_text_1bit(row["name"], FOOD_BITMAP_WIDTH, FOOD_BITMAP_HEIGHT, font)
            items.append(
                PushPreviewItem(
                    slot=index,
                    name=row["name"],
                    category=row["category"],
                    device_type=1 if row["category"] == DRINK_CATEGORY else 0,
                    expiry_date=row["expiry_date"],
                    days_remaining=row["days_remaining"],
                    bitmap_ok=len(bitmap) == (FOOD_BITMAP_WIDTH + 7) // 8 * FOOD_BITMAP_HEIGHT,
                )
            )
        return items

    @router.get("/epd/preview.png", tags=["epd"])
    def push_preview_png(request: Request):
        """生成 800x480 看板预览 PNG（与固件布局对齐的简化版）。"""
        import io

        from PIL import Image, ImageDraw

        cfg = request.app.state.cfg
        db = request.app.state.db
        font = find_font(cfg.font_path)
        if font is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "未找到可用中文字体，无法渲染预览",
            )
        from PIL import ImageFont

        font_path = font  # find_font 返回的是路径字符串
        font = ImageFont.truetype(font_path, 20)
        font_big = ImageFont.truetype(font_path, 28)
        img = Image.new("RGB", (800, 480), "white")
        draw = ImageDraw.Draw(img)
        # 左侧日历占位（与固件布局一致的简单日历）
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).astimezone()
        draw.text((40, 40), f"{now.year}年{now.month}月", fill="black", font=font_big)
        draw.text((40, 90), f"{now.day}日 周{'一二三四五六日'[now.weekday()]}", fill="black", font=font)
        draw.line((424, 10, 424, 470), fill="black", width=2)
        # 右侧食品面板
        draw.rectangle((430, 190, 790, 470), outline="black", width=1)
        draw.text((444, 200), "食品到期", fill="black", font=font_big)
        draw.line((442, 232, 718, 232), fill="red", width=2)
        for index, row in enumerate(db.top_for_display(limit=4)):
            bitmap = render_text_1bit(row["name"], FOOD_BITMAP_WIDTH, FOOD_BITMAP_HEIGHT, font_path)
            tile = Image.frombytes("1", (FOOD_BITMAP_WIDTH, FOOD_BITMAP_HEIGHT), bytes(bitmap))
            img.paste(tile.convert("L"), (488, 246 + index * 64))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return HTTPResponse(content=buf.getvalue(), media_type="image/png")

    return router
