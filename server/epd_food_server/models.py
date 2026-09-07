"""Pydantic 模型：入参校验 + 出参序列化。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import DRINK_CATEGORY

FoodOrder = Literal["expiry", "created", "name"]
FoodStatus = Literal["active", "consumed", "all"]


class FoodIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="食品名称")
    category: str = Field(min_length=1, max_length=32, default="食品", description="品类型")
    production_date: date = Field(description="生产日期")
    shelf_life_days: int = Field(ge=1, le=36500, description="保质期（天）")
    quantity: int = Field(ge=1, le=9999, default=1, description="数量")


class FoodPatch(BaseModel):
    name: str | None = Field(min_length=1, max_length=64, default=None)
    category: str | None = Field(min_length=1, max_length=32, default=None)
    production_date: date | None = None
    shelf_life_days: int | None = Field(ge=1, le=36500, default=None)
    quantity: int | None = Field(ge=1, le=9999, default=None)


class FoodOut(BaseModel):
    id: int
    name: str
    category: str
    production_date: date
    shelf_life_days: int
    quantity: int
    expiry_date: date
    days_remaining: int
    device_type: int  # 0=食品 1=饮品（与墨水屏协议对应）
    interaction_count: int
    created_at: datetime
    updated_at: datetime
    consumed_at: datetime | None


class PushPreviewItem(BaseModel):
    slot: int
    name: str
    category: str
    device_type: int
    expiry_date: date
    days_remaining: int
    bitmap_ok: bool


def food_out(row: dict[str, Any]) -> FoodOut:
    return FoodOut(
        id=row["id"],
        name=row["name"],
        category=row["category"],
        production_date=row["production_date"],
        shelf_life_days=row["shelf_life_days"],
        quantity=row["quantity"],
        expiry_date=row["expiry_date"],
        days_remaining=row["days_remaining"],
        device_type=1 if row["category"] == DRINK_CATEGORY else 0,
        interaction_count=row["interaction_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        consumed_at=row["consumed_at"],
    )
