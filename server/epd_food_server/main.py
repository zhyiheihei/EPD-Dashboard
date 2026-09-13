"""服务端入口：`epd-food-server serve | push-now`。"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys

from . import __version__
from .config import Config, ensure_state_dir
from .db import Database

log = logging.getLogger("epd_food_server")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    if not verbose:
        logging.getLogger("bleak").setLevel(logging.WARNING)


def build_runtime(cfg: Config | None = None) -> tuple[Config, Database]:
    cfg = cfg or Config.from_env()
    ensure_state_dir(cfg)
    return cfg, Database(cfg.dsn)


def serve(cfg: Config, db: Database) -> None:
    import uvicorn

    from .api import build_app
    from .ota import OtaRunner, OtaStore
    from .pusher import Pusher

    app = build_app(cfg, db)
    store = OtaStore(cfg, db)
    app.state.pusher = Pusher(cfg, db)
    app.state.ota_store = store
    app.state.ota_runner = OtaRunner(cfg, store)

    try:
        db.ensure_schema()
    except Exception as exc:
        log.error("数据库不可用（%s），请检查 EPD_FOOD_DSN 与 PostgreSQL 服务", exc)
        raise SystemExit(1) from exc

    _log_serve_banner(cfg)
    uvicorn.run(
        app,
        host=cfg.bind_host,
        port=cfg.bind_port,
        log_level="info",
        access_log=False,
    )


def _log_serve_banner(cfg: Config) -> None:
    log.info(
        "epd_food_server %s 启动: %s:%s state=%s",
        __version__, cfg.bind_host, cfg.bind_port, cfg.state_dir,
    )


def push_now(cfg: Config, db: Database) -> int:
    """供 systemd timer 调用：直接推送一次，成功返回 0。"""
    from .pusher import Pusher

    db.ensure_schema()
    pusher = Pusher(cfg, db)
    status = asyncio.run(pusher.push(reason="timer"))
    if status.ok:
        log.info("推送成功: 设备=%s 条目=%s 耗时=%ss", status.device, status.items, status.duration_s)
        return 0
    log.error("推送失败: %s", status.error)
    return 1


def ota_push(cfg: Config, db: Database, zip_path: str) -> int:
    """对设备执行 OTA 升级（包先入库再升级），成功返回 0。"""
    from pathlib import Path

    from .ota import OtaPackageError, OtaRunner, OtaStore

    db.ensure_schema()
    store = OtaStore(cfg, db)
    try:
        row = store.save_from_upload(Path(zip_path).name, Path(zip_path).read_bytes())
    except OtaPackageError as exc:
        log.error("OTA 包无效: %s", exc)
        return 2
    log.info(
        "固件包: %s (app_version=%s, %s bytes)", row["filename"], row["app_version"], row["size"]
    )
    runner = OtaRunner(cfg, store)
    result = asyncio.run(runner.upgrade(row))
    if result.get("ok"):
        log.info("OTA 升级成功: 设备=%s 耗时=%ss", result.get("device"), result.get("duration_s"))
        return 0
    log.error("OTA 升级失败: %s", result.get("error"))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="epd-food-server", description="家庭食品存储看板服务端")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    sub = parser.add_subparsers(dest="command", required=True)
    serve_parser = sub.add_parser("serve", help="启动 API 服务")
    serve_parser.add_argument("--host", default=None, help="覆盖 EPD_FOOD_BIND_HOST")
    serve_parser.add_argument("--port", type=int, default=None, help="覆盖 EPD_FOOD_BIND_PORT")
    sub.add_parser("push-now", help="立即向墨水屏推送一次")
    ota_parser = sub.add_parser("ota-push", help="对墨水屏执行固件 OTA 升级")
    ota_parser.add_argument("zip", help="nrfutil 生成的 *-ota.zip 包路径")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    cfg, db = build_runtime()

    if args.command == "serve":
        overrides = {}
        if args.host:
            overrides["bind_host"] = args.host
        if args.port:
            overrides["bind_port"] = args.port
        if overrides:
            cfg = dataclasses.replace(cfg, **overrides)
        serve(cfg, db)
        return 0
    if args.command == "push-now":
        return push_now(cfg, db)
    if args.command == "ota-push":
        return ota_push(cfg, db, args.zip)
    parser.error(f"未知命令: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
