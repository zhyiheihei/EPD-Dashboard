"""PostgreSQL 数据访问（psycopg3，Unix socket 对等认证，线程本地连接）。"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS foods (
        id SERIAL PRIMARY KEY,
        name VARCHAR(64) NOT NULL,
        category VARCHAR(32) NOT NULL DEFAULT '食品',
        production_date DATE NOT NULL,
        shelf_life_days INTEGER NOT NULL CHECK (shelf_life_days BETWEEN 1 AND 36500),
        quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity BETWEEN 1 AND 9999),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        consumed_at TIMESTAMPTZ
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS foods_active_expiry_idx
        ON foods ((production_date + shelf_life_days))
        WHERE consumed_at IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS firmware_packages (
        id SERIAL PRIMARY KEY,
        filename VARCHAR(255) NOT NULL,
        app_version INTEGER,
        device_type INTEGER,
        softdevice_required VARCHAR(32),
        size INTEGER NOT NULL,
        sha256 VARCHAR(64) NOT NULL UNIQUE,
        stored_path VARCHAR(512) NOT NULL,
        uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)

VALID_ORDERS = {
    "expiry": "(production_date + shelf_life_days) ASC, created_at DESC",
    "created": "created_at DESC",
    "name": "name ASC",
}


class Database:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._local = threading.local()

    def _conn(self) -> psycopg.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            conn = psycopg.connect(self._dsn, row_factory=dict_row, autocommit=True)
            self._local.conn = conn
        return conn

    def check(self) -> bool:
        with self._conn().cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() is not None

    def ensure_schema(self) -> None:
        with self._conn().cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)
        log.info("数据库 schema 就绪")

    # ---------- 查询 ----------

    @staticmethod
    def _row_to_food(row: dict[str, Any], today: date) -> dict[str, Any]:
        expiry: date = row["expiry_date"]
        return {**row, "days_remaining": (expiry - today).days}

    def list_foods(self, status: str = "active", order: str = "expiry") -> list[dict[str, Any]]:
        where = {
            "active": "WHERE consumed_at IS NULL",
            "consumed": "WHERE consumed_at IS NOT NULL",
            "all": "",
        }.get(status, "WHERE consumed_at IS NULL")
        order_sql = VALID_ORDERS.get(order, VALID_ORDERS["expiry"])
        with self._conn().cursor() as cur:
            cur.execute(
                f"""
                SELECT id, name, category, production_date, shelf_life_days, quantity,
                       (production_date + shelf_life_days)::date AS expiry_date,
                       created_at, updated_at, consumed_at
                FROM foods {where} ORDER BY {order_sql}
                """
            )
            rows = cur.fetchall()
        today = datetime.now(timezone.utc).date()
        return [self._row_to_food(row, today) for row in rows]

    def get_food(self, food_id: int) -> dict[str, Any] | None:
        with self._conn().cursor() as cur:
            cur.execute(
                """
                SELECT id, name, category, production_date, shelf_life_days, quantity,
                       (production_date + shelf_life_days)::date AS expiry_date,
                       created_at, updated_at, consumed_at
                FROM foods WHERE id = %s
                """,
                (food_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_food(row, datetime.now(timezone.utc).date())

    def top_for_display(self, limit: int = 4) -> list[dict[str, Any]]:
        return self.list_foods(status="active", order="expiry")[:limit]

    def stats(self) -> dict[str, Any]:
        with self._conn().cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS total_active,
                       count(*) FILTER (WHERE production_date + shelf_life_days < CURRENT_DATE) AS expired,
                       count(*) FILTER (WHERE production_date + shelf_life_days >= CURRENT_DATE
                                        AND production_date + shelf_life_days < CURRENT_DATE + 3) AS expiring_3d,
                       count(*) FILTER (WHERE production_date + shelf_life_days >= CURRENT_DATE
                                        AND production_date + shelf_life_days < CURRENT_DATE + 7) AS expiring_7d,
                       count(*) FILTER (WHERE consumed_at IS NOT NULL) AS total_consumed
                FROM foods
                """
            )
            totals = dict(cur.fetchone())
            cur.execute(
                """
                SELECT category, count(*) AS count
                FROM foods WHERE consumed_at IS NULL
                GROUP BY category ORDER BY count DESC, category ASC
                """
            )
            totals["by_category"] = cur.fetchall()
        return totals

    # ---------- 写入 ----------

    def insert_food(
        self,
        name: str,
        category: str,
        production_date: date,
        shelf_life_days: int,
        quantity: int,
    ) -> dict[str, Any]:
        with self._conn().cursor() as cur:
            cur.execute(
                """
                INSERT INTO foods (name, category, production_date, shelf_life_days, quantity)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (name, category, production_date, shelf_life_days, quantity),
            )
            food_id = cur.fetchone()["id"]
        return self.get_food(food_id)  # type: ignore[return-value]

    def update_food(self, food_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"name", "category", "production_date", "shelf_life_days", "quantity"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return self.get_food(food_id)
        sets = ", ".join(f"{column} = %s" for column in updates)
        with self._conn().cursor() as cur:
            cur.execute(
                f"UPDATE foods SET {sets}, updated_at = now() WHERE id = %s",
                (*updates.values(), food_id),
            )
        return self.get_food(food_id)

    def set_consumed(self, food_id: int, consumed: bool) -> dict[str, Any] | None:
        with self._conn().cursor() as cur:
            cur.execute(
                """
                UPDATE foods
                SET consumed_at = CASE WHEN %s THEN now() ELSE NULL END,
                    updated_at = now()
                WHERE id = %s
                """,
                (consumed, food_id),
            )
        return self.get_food(food_id)

    def delete_food(self, food_id: int) -> bool:
        with self._conn().cursor() as cur:
            cur.execute("DELETE FROM foods WHERE id = %s", (food_id,))
            return cur.rowcount > 0

    # ---------- 固件 OTA 包 ----------

    def insert_firmware(
        self,
        filename: str,
        app_version: int | None,
        device_type: int | None,
        softdevice_required: str | None,
        size: int,
        sha256: str,
        stored_path: str,
    ) -> dict[str, Any]:
        with self._conn().cursor() as cur:
            cur.execute(
                """
                INSERT INTO firmware_packages
                    (filename, app_version, device_type, softdevice_required, size, sha256, stored_path)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sha256) DO UPDATE SET filename = EXCLUDED.filename
                RETURNING id, filename, app_version, device_type, softdevice_required,
                          size, sha256, stored_path, uploaded_at
                """,
                (filename, app_version, device_type, softdevice_required, size, sha256, stored_path),
            )
            return dict(cur.fetchone())

    def list_firmwares(self) -> list[dict[str, Any]]:
        with self._conn().cursor() as cur:
            cur.execute(
                """
                SELECT id, filename, app_version, device_type, softdevice_required,
                       size, sha256, stored_path, uploaded_at
                FROM firmware_packages
                ORDER BY app_version DESC NULLS LAST, uploaded_at DESC
                """
            )
            return cur.fetchall()

    def get_firmware(self, firmware_id: int) -> dict[str, Any] | None:
        with self._conn().cursor() as cur:
            cur.execute(
                """
                SELECT id, filename, app_version, device_type, softdevice_required,
                       size, sha256, stored_path, uploaded_at
                FROM firmware_packages WHERE id = %s
                """,
                (firmware_id,),
            )
            return cur.fetchone()

    def get_firmware_by_sha(self, sha256: str) -> dict[str, Any] | None:
        with self._conn().cursor() as cur:
            cur.execute(
                """
                SELECT id, filename, app_version, device_type, softdevice_required,
                       size, sha256, stored_path, uploaded_at
                FROM firmware_packages WHERE sha256 = %s
                """,
                (sha256,),
            )
            return cur.fetchone()

    def latest_firmware(self) -> dict[str, Any] | None:
        with self._conn().cursor() as cur:
            cur.execute(
                """
                SELECT id, filename, app_version, device_type, softdevice_required,
                       size, sha256, stored_path, uploaded_at
                FROM firmware_packages
                ORDER BY app_version DESC NULLS LAST, uploaded_at DESC
                LIMIT 1
                """
            )
            return cur.fetchone()

    def delete_firmware(self, firmware_id: int) -> dict[str, Any] | None:
        row = self.get_firmware(firmware_id)
        if row is None:
            return None
        with self._conn().cursor() as cur:
            cur.execute("DELETE FROM firmware_packages WHERE id = %s", (firmware_id,))
        return row
