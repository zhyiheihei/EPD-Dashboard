"""OTA 固件管理 API：上传/列表/删除、设备版本读取、触发升级、状态查询。"""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..ble import DeviceError, read_app_version
from ..ota import OtaPackageError

__all__ = ["create_router"]


def _row_meta(row: dict) -> dict:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "app_version": row["app_version"],
        "device_type": row["device_type"],
        "softdevice_required": row["softdevice_required"],
        "size": row["size"],
        "sha256": row["sha256"],
        "uploaded_at": row["uploaded_at"].isoformat() if row.get("uploaded_at") else None,
    }


def create_router(auth: Callable) -> APIRouter:
    router = APIRouter(dependencies=[Depends(auth)])

    @router.post("/ota/firmware", status_code=status.HTTP_201_CREATED, tags=["ota"])
    async def upload_firmware(request: Request, filename: str = "firmware.zip"):
        """原始二进制上传 OTA zip：请求体即文件内容，文件名经 query 传递。"""
        if not filename.lower().endswith(".zip"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "请上传 .zip OTA 包")
        content = await request.body()
        if not content:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "请求体为空")
        try:
            row = request.app.state.ota_store.save_from_upload(filename, content)
        except OtaPackageError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return _row_meta(row)

    @router.get("/ota/firmware", tags=["ota"])
    def list_firmware(request: Request):
        return [_row_meta(row) for row in request.app.state.db.list_firmwares()]

    @router.delete("/ota/firmware/{firmware_id}", tags=["ota"])
    def delete_firmware(firmware_id: int, request: Request):
        row = request.app.state.db.delete_firmware(firmware_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "固件包不存在")
        from pathlib import Path

        try:
            Path(row["stored_path"]).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return {"deleted": True, "id": firmware_id}

    @router.get("/ota/device", tags=["ota"])
    async def device_version(request: Request):
        """实时连接设备读取当前固件版本。"""
        cfg = request.app.state.cfg
        try:
            name, version = await read_app_version(cfg)
        except DeviceError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        return {"device": name, "version": version, "version_hex": f"0x{version:02X}"}

    @router.post("/ota/push", tags=["ota"])
    async def push_firmware(request: Request, firmware_id: int | None = None):
        """对设备执行 OTA 升级；缺省取最新版本包。立即返回，结果经 status 轮询。"""
        import asyncio

        db = request.app.state.db
        row = db.get_firmware(firmware_id) if firmware_id else db.latest_firmware()
        if row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "未找到固件包，请先上传" if firmware_id is None else "固件包不存在",
            )
        runner = request.app.state.ota_runner
        asyncio.create_task(runner.upgrade(row))
        return {
            "started": True,
            "busy": False,
            "firmware": {
                "id": row["id"],
                "filename": row["filename"],
                "app_version": row["app_version"],
            },
        }

    @router.get("/ota/status", tags=["ota"])
    def ota_status(request: Request):
        runner = request.app.state.ota_runner
        return runner.read_status()

    return router
