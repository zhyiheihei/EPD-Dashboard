"""食品 CRUD REST API。所有写操作在提交后触发防抖推送（可配置）。"""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, status

from .deps import notify_change
from ..models import FoodIn, FoodOut, FoodPatch, food_out


def create_router(auth: Callable) -> APIRouter:
    router = APIRouter(dependencies=[Depends(auth)])

    @router.get("/foods", response_model=list[FoodOut], tags=["foods"])
    def list_foods(
        request: Request,
        status_filter: str = "active",
        order: str = "expiry",
    ):
        db = request.app.state.db
        if status_filter not in ("active", "consumed", "all"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "status_filter 取值非法")
        return [food_out(row) for row in db.list_foods(status_filter, order)]

    @router.get("/foods/stats", tags=["foods"])
    def food_stats(request: Request):
        return request.app.state.db.stats()

    @router.get("/foods/{food_id}", response_model=FoodOut, tags=["foods"])
    def get_food(food_id: int, request: Request):
        row = request.app.state.db.get_food(food_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "食品不存在")
        return food_out(row)

    @router.post(
        "/foods", response_model=FoodOut, status_code=status.HTTP_201_CREATED, tags=["foods"]
    )
    def create_food(body: FoodIn, request: Request):
        row = request.app.state.db.insert_food(
            name=body.name.strip(),
            category=body.category.strip(),
            production_date=body.production_date,
            shelf_life_days=body.shelf_life_days,
            quantity=body.quantity,
        )
        notify_change(request)
        return food_out(row)

    @router.patch("/foods/{food_id}", response_model=FoodOut, tags=["foods"])
    def update_food(food_id: int, body: FoodPatch, request: Request):
        db = request.app.state.db
        if db.get_food(food_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "食品不存在")
        fields = body.model_dump(exclude_none=True)
        if "name" in fields:
            fields["name"] = fields["name"].strip()
        if "category" in fields:
            fields["category"] = fields["category"].strip()
        row = db.update_food(food_id, fields)
        notify_change(request)
        return food_out(row)

    @router.delete("/foods/{food_id}", response_model=FoodOut, tags=["foods"])
    def delete_food(food_id: int, request: Request, hard: bool = False):
        """默认软删除（记 consumed_at，不上屏）；hard=true 物理删除。"""
        db = request.app.state.db
        row = db.get_food(food_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "食品不存在")
        if hard:
            db.delete_food(food_id)
            notify_change(request)
            return food_out(row)
        updated = db.set_consumed(food_id, True)
        notify_change(request)
        return food_out(updated)

    @router.post("/foods/{food_id}/restore", response_model=FoodOut, tags=["foods"])
    def restore_food(food_id: int, request: Request):
        db = request.app.state.db
        if db.get_food(food_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "食品不存在")
        updated = db.set_consumed(food_id, False)
        notify_change(request)
        return food_out(updated)

    return router
