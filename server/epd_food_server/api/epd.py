"""推送控制 API：手动推送、状态查询、上屏预览。"""

from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, status

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

    return router
