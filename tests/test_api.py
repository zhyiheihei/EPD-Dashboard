"""REST API 测试：FakeDB + httpx ASGI（不依赖真实 PostgreSQL）。"""

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from epd_food_server import render
from epd_food_server.api import build_app
from epd_food_server.config import Config

TOKEN = "unit-test-token"


def _row(id: int, name: str, days: int, category: str = "食品", quantity: int = 1, consumed=None):
    production = date(2026, 9, 1)
    expiry = production + timedelta(days=days)
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    return {
        "id": id,
        "name": name,
        "category": category,
        "production_date": production,
        "shelf_life_days": days,
        "quantity": quantity,
        "expiry_date": expiry,
        "days_remaining": (expiry - date(2026, 9, 7)).days,
        "created_at": now,
        "updated_at": now,
        "consumed_at": consumed,
    }


class FakeDB:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.firmwares = []
        self._fw_next_id = 1

    def check(self):
        return True

    def ensure_schema(self):
        pass

    def list_foods(self, status="active", order="expiry"):
        rows = list(self.rows)
        if status == "active":
            rows = [r for r in rows if r["consumed_at"] is None]
        elif status == "consumed":
            rows = [r for r in rows if r["consumed_at"] is not None]
        if order == "expiry":
            rows.sort(key=lambda r: r["expiry_date"])
        return rows

    def get_food(self, food_id):
        return next((r for r in self.rows if r["id"] == food_id), None)

    def insert_food(self, name, category, production_date, shelf_life_days, quantity):
        row = _row(len(self.rows) + 1, name, shelf_life_days, category, quantity)
        row["production_date"] = production_date
        row["expiry_date"] = production_date + timedelta(days=shelf_life_days)
        row["days_remaining"] = (row["expiry_date"] - date(2026, 9, 7)).days
        self.rows.append(row)
        return row

    def update_food(self, food_id, fields):
        row = self.get_food(food_id)
        if row is None:
            return None
        row.update(fields)
        row["expiry_date"] = row["production_date"] + timedelta(days=row["shelf_life_days"])
        row["days_remaining"] = (row["expiry_date"] - date(2026, 9, 7)).days
        return row

    def set_consumed(self, food_id, consumed):
        row = self.get_food(food_id)
        if row is None:
            return None
        row["consumed_at"] = datetime.now(timezone.utc) if consumed else None
        return row

    def delete_food(self, food_id):
        before = len(self.rows)
        self.rows = [r for r in self.rows if r["id"] != food_id]
        return len(self.rows) < before

    def stats(self):
        return {
            "total_active": 0,
            "expired": 0,
            "expiring_3d": 0,
            "expiring_7d": 0,
            "total_consumed": 0,
            "by_category": [],
        }

    def top_for_display(self, limit=4):
        return self.list_foods("active", "expiry")[:limit]

    # ---- 固件 OTA ----

    def insert_firmware(self, filename, app_version, device_type, softdevice_required, size, sha256, stored_path):
        existing = self.get_firmware_by_sha(sha256)
        if existing is not None:
            return existing
        from datetime import datetime as _dt

        row = {
            "id": self._fw_next_id,
            "filename": filename,
            "app_version": app_version,
            "device_type": device_type,
            "softdevice_required": softdevice_required,
            "size": size,
            "sha256": sha256,
            "stored_path": stored_path,
            "uploaded_at": _dt.now(timezone.utc),
        }
        self._fw_next_id += 1
        self.firmwares.append(row)
        return row

    def list_firmwares(self):
        return sorted(self.firmwares, key=lambda r: (r["app_version"] or 0), reverse=True)

    def get_firmware(self, firmware_id):
        return next((r for r in self.firmwares if r["id"] == firmware_id), None)

    def get_firmware_by_sha(self, sha256):
        return next((r for r in self.firmwares if r["sha256"] == sha256), None)

    def latest_firmware(self):
        return self.list_firmwares()[0] if self.firmwares else None

    def delete_firmware(self, firmware_id):
        row = self.get_firmware(firmware_id)
        if row is None:
            return None
        self.firmwares = [r for r in self.firmwares if r["id"] != firmware_id]
        return row


class FakePusher:
    def __init__(self):
        self.requests = 0
        self.in_progress = False

    def request_change_push(self):
        self.requests += 1
        return True

    async def push(self, reason):
        return {"ok": True}

    def read_status(self):
        return {"in_progress": self.in_progress, "ok": None}


class FakeOtaRunner:
    def __init__(self):
        self.upgraded = []
        self.in_progress = False

    async def upgrade(self, row):
        self.upgraded.append(row["id"])
        return {"ok": True, "device": "DfuTarg"}

    def read_status(self):
        return {"in_progress": self.in_progress, "ok": None}


def make_app(db=None, token: str = TOKEN):
    import tempfile
    from pathlib import Path

    from epd_food_server.ota import OtaStore

    cfg = Config(
        dsn="",
        api_token=token,
        bind_host="127.0.0.1",
        bind_port=0,
        timezone="Asia/Shanghai",
        font_path=None,
        device_name_prefix="NRF_EPD",
        device_address=None,
        scan_timeout=1,
        connect_timeout=1,
        session_timeout=1,
        max_chunk=0,
        settle_seconds=0,
        push_retries=0,
        retry_backoff=0,
        push_on_change=True,
        push_debounce=0,
        change_min_interval=0,
        commit_sleep=True,
        full_refresh_every=8,
        state_dir=Path(tempfile.mkdtemp(prefix="epd-food-test-")),
    )
    app = build_app(cfg, db or FakeDB())
    app.state.pusher = FakePusher()
    app.state.ota_store = OtaStore(cfg, app.state.db)
    app.state.ota_runner = FakeOtaRunner()
    return app


def make_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def auth_header(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


async def test_health():
    async with make_client(make_app()) as client:
        resp = await client.get("/api/health", headers=auth_header())
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


async def test_auth_rejects_missing_token():
    async with make_client(make_app()) as client:
        assert (await client.get("/api/foods")).status_code == 401
        wrong = {"Authorization": "Bearer wrong"}
        assert (await client.get("/api/foods", headers=wrong)).status_code == 401
        assert (await client.get("/api/foods", headers=auth_header())).status_code == 200


async def test_no_token_configured_allows_anonymous():
    async with make_client(make_app(token="")) as client:
        assert (await client.get("/api/foods")).status_code == 200


async def test_food_crud_cycle():
    async with make_client(make_app()) as client:
        created = await client.post(
            "/api/foods",
            headers=auth_header(),
            json={
                "name": "鲜牛奶",
                "category": "饮品",
                "production_date": "2026-09-01",
                "shelf_life_days": 7,
                "quantity": 2,
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert body["expiry_date"] == "2026-09-08"
        assert body["device_type"] == 1  # 饮品 → 协议 type=1
        food_id = body["id"]

        patched = await client.patch(
            f"/api/foods/{food_id}", headers=auth_header(), json={"quantity": 5}
        )
        assert patched.json()["quantity"] == 5

        deleted = await client.delete(f"/api/foods/{food_id}", headers=auth_header())
        assert deleted.json()["consumed_at"] is not None

        listing = (await client.get("/api/foods", headers=auth_header())).json()
        assert all(item["id"] != food_id for item in listing)

        consumed = await client.get("/api/foods?status_filter=consumed", headers=auth_header())
        assert any(item["id"] == food_id for item in consumed.json())


async def test_create_triggers_change_push():
    import asyncio

    app = make_app()
    async with make_client(app) as client:
        app.state.loop = asyncio.get_running_loop()  # ASGITransport 不跑 lifespan
        await client.post(
            "/api/foods",
            headers=auth_header(),
            json={
                "name": "酸奶",
                "category": "乳制品",
                "production_date": "2026-09-05",
                "shelf_life_days": 21,
            },
        )
        # notify_change 经 loop.call_soon_threadsafe 调度，等待其落地
        await asyncio.sleep(0.05)
        assert app.state.pusher.requests == 1


async def test_validation_rejects_bad_input():
    async with make_client(make_app()) as client:
        resp = await client.post(
            "/api/foods",
            headers=auth_header(),
            json={
                "name": "",
                "category": "食品",
                "production_date": "2026-09-01",
                "shelf_life_days": 7,
            },
        )
        assert resp.status_code == 422


async def test_404_for_missing_food():
    async with make_client(make_app()) as client:
        resp = await client.get("/api/foods/999", headers=auth_header())
        assert resp.status_code == 404


async def test_meta():
    async with make_client(make_app()) as client:
        meta = (await client.get("/api/meta", headers=auth_header())).json()
        assert "饮品" in meta["categories"]


async def test_preview():
    font = render.find_font()
    if font is None:
        pytest.skip("本机无 CJK 字体，渲染预览需要字体")
    db = FakeDB([_row(1, "面包", 3), _row(2, "鲜牛奶", 7, "饮品")])
    app = make_app(db)
    # Config 是 frozen dataclass，直接替换 font_path
    import dataclasses

    app.state.cfg = dataclasses.replace(app.state.cfg, font_path=font)
    async with make_client(app) as client:
        resp = await client.get("/api/epd/preview", headers=auth_header())
        items = resp.json()
        assert len(items) == 2
        assert items[0]["name"] == "面包"  # 3 天后到期，排在前面
        assert items[0]["slot"] == 0
        assert items[1]["device_type"] == 1


async def test_push_endpoint_dispatches():
    app = make_app()
    async with make_client(app) as client:
        resp = await client.post("/api/epd/push", headers=auth_header())
        assert resp.status_code == 200
        assert resp.json()["started"] is True
        status = (await client.get("/api/epd/status", headers=auth_header())).json()
        assert "in_progress" in status


# ===================== OTA 固件 =====================

async def test_ota_upload_list_dedup_push():
    from conftest import make_ota_zip_bytes

    app = make_app()
    async with make_client(app) as client:
        zip_bytes = make_ota_zip_bytes(app_version=31, bin_size=1024)

        uploaded = await client.post(
            "/api/ota/firmware?filename=EPD-nRF52-uc8179-v1F-ota.zip",
            headers=auth_header(),
            content=zip_bytes,
        )
        assert uploaded.status_code == 201, uploaded.text
        meta = uploaded.json()
        assert meta["app_version"] == 31
        assert meta["filename"].endswith(".zip")

        # 同内容重复上传 → 幂等（同一 id）
        again = await client.post(
            "/api/ota/firmware?filename=again.zip", headers=auth_header(), content=zip_bytes
        )
        assert again.json()["id"] == meta["id"]

        listing = (await client.get("/api/ota/firmware", headers=auth_header())).json()
        assert len(listing) == 1

        # 推送（默认最新包）
        import asyncio

        pushed = await client.post("/api/ota/push", headers=auth_header())
        assert pushed.status_code == 200
        assert pushed.json()["started"] is True
        await asyncio.sleep(0.05)  # 等后台任务落地
        assert pushed.json()["firmware"]["id"] == meta["id"]
        assert app.state.ota_runner.upgraded == [meta["id"]]

        # 指定不存在的包
        missing = await client.post(
            "/api/ota/push?firmware_id=999", headers=auth_header()
        )
        assert missing.status_code == 404


async def test_ota_upload_validation():
    from conftest import make_ota_zip_bytes

    async with make_client(make_app()) as client:
        # 非 zip 文件名
        bad_name = await client.post(
            "/api/ota/firmware?filename=firmware.bin",
            headers=auth_header(),
            content=b"\x00\x01",
        )
        assert bad_name.status_code == 400
        # 不是 OTA 包的 zip
        bad_zip = await client.post(
            "/api/ota/firmware?filename=not-ota.zip",
            headers=auth_header(),
            content=b"PK\x03\x04 not a real dfu package",
        )
        assert bad_zip.status_code == 400
        # 空 body
        empty = await client.post(
            "/api/ota/firmware?filename=x.zip", headers=auth_header(), content=b""
        )
        assert empty.status_code == 400


async def test_ota_delete():
    from conftest import make_ota_zip_bytes

    app = make_app()
    async with make_client(app) as client:
        await client.post(
            "/api/ota/firmware?fw.zip", headers=auth_header(),
            content=make_ota_zip_bytes(),
        )
        listed = (await client.get("/api/ota/firmware", headers=auth_header())).json()
        deleted = await client.delete(
            f"/api/ota/firmware/{listed[0]['id']}", headers=auth_header()
        )
        assert deleted.json()["deleted"] is True
        assert (await client.get("/api/ota/firmware", headers=auth_header())).json() == []
