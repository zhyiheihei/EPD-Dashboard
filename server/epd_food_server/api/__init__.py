"""FastAPI 应用工厂。路由通过 request.app.state 访问 cfg / db / pusher。"""

from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import RedirectResponse

from ..config import PRESET_CATEGORIES, Config
from ..db import Database
from ..render import find_font
from . import epd as epd_api
from . import foods as foods_api
from . import ota as ota_api

__all__ = ["build_app", "make_auth_dependency"]

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def make_auth_dependency(cfg: Config):
    """Bearer Token 鉴权依赖；token 未配置时放行（开发模式）。"""

    def dependency(authorization: str | None = Header(default=None)) -> None:
        if not cfg.api_token:
            return
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, cfg.api_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效或缺失的访问令牌",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return dependency


def build_app(cfg: Config, db: Database) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.loop = asyncio.get_running_loop()
        yield

    app = FastAPI(
        title="EPD Food Dashboard API",
        version="0.1.0",
        description="家庭食品存储看板服务端",
        lifespan=lifespan,
    )
    app.state.cfg = cfg
    app.state.db = db

    auth = make_auth_dependency(cfg)
    app.include_router(foods_api.create_router(auth), prefix="/api")
    app.include_router(epd_api.create_router(auth), prefix="/api")
    app.include_router(ota_api.create_router(auth), prefix="/api")

    @app.get("/api/health", tags=["meta"])
    def health() -> dict:
        db_ok = True
        error: str | None = None
        try:
            db.check()
        except Exception as exc:  # noqa: BLE001
            db_ok = False
            error = str(exc)
        return {"ok": db_ok, "db": db_ok, "error": error}

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse(url="/ui/")

    @app.get("/api/meta", tags=["meta"])
    def meta() -> dict:
        font = find_font(cfg.font_path)
        return {
            "categories": PRESET_CATEGORIES,
            "font_available": font is not None,
            "font_path": font,
        }

    # WebUI 静态页面：无鉴权（页面本身无数据，API 另行鉴权）
    if STATIC_DIR.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

    return app
