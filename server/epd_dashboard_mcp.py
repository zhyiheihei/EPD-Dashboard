"""EPD-Dashboard MCP server：让 agent 通过 MCP 直接管理食品条目。

stdio 传输，仅封装服务端 REST API（食品 CRUD），不含推送/OTA——
录入与修改后由服务端自动防抖推送到墨水屏。

环境变量：
    EPD_MCP_BASE_URL  服务端地址（默认 http://127.0.0.1:18313）
    EPD_MCP_TOKEN     API 令牌（服务端开启鉴权时必填）

配置示例（pi / Claude Desktop 等宿主）：
    {
      "mcpServers": {
        "epd-dashboard": {
          "command": "/path/to/EPD-Dashboard/.venv/bin/python",
          "args": ["-m", "epd_dashboard_mcp"],
          "cwd": "/path/to/EPD-Dashboard/server",
          "env": {"EPD_MCP_BASE_URL": "http://127.0.0.1:18313"}
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer

BASE_URL = os.environ.get("EPD_MCP_BASE_URL", "http://127.0.0.1:18313").rstrip("/")
TOKEN = os.environ.get("EPD_MCP_TOKEN", "")

app: MCPServer = MCPServer("epd-dashboard", instructions="管理冰箱食品库存（EPD 墨水屏看板）。录入/修改后会自动防抖推送到墨水屏，无需额外操作。")


def api(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE_URL + "/api" + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"}
        | ({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:200]
        raise RuntimeError(f"服务端返回 {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接服务端 {BASE_URL}（{exc.reason}）") from exc


def summarize(rows: list[dict], include_eaten: bool) -> str:
    lines = []
    for row in rows:
        if not include_eaten and row.get("consumed_at"):
            continue
        days = row.get("days_remaining")
        if days is None:
            expiry = date.fromisoformat(row["production_date"]) + timedelta(
                days=row["shelf_life_days"]
            )
            days = (expiry - date.today()).days
        state = "已吃完" if row.get("consumed_at") else (f"剩 {days} 天" if days >= 0 else f"已过期 {-days} 天")
        kind = "饮品" if row.get("category") == "drink" else "食品"
        lines.append(f"#{row['id']} {row['name']} ×{row.get('quantity', 1)}（{kind}）{state}")
    return "\n".join(lines) if lines else "（空）"


@app.add_tool
def list_foods(include_eaten: bool = False) -> str:
    """列出库存食品/饮品，含到期天数与是否已吃完。"""
    rows = api("GET", "/foods")
    return summarize(rows, include_eaten)


@app.add_tool
def add_food(
    name: str,
    shelf_life_days: int,
    category: str = "food",
    quantity: int = 1,
    production_date: str = "",
) -> str:
    """新增食品或饮品。category: food(食品)/drink(饮品)；production_date: YYYY-MM-DD，默认今天。录入后自动推送到墨水屏。"""
    if category not in ("food", "drink"):
        return "参数错误：category 必须是 food 或 drink"
    row = api("POST", "/foods", {
        "name": name.strip(),
        "category": category,
        "quantity": max(1, quantity),
        "production_date": production_date or datetime.now().strftime("%Y-%m-%d"),
        "shelf_life_days": shelf_life_days,
    })
    return f"已添加：{summarize([row], include_eaten=True)}\n（稍后自动推送到墨水屏）"


@app.add_tool
def update_food(
    food_id: int,
    name: str = "",
    category: str = "",
    quantity: int = 0,
    production_date: str = "",
    shelf_life_days: int = 0,
) -> str:
    """修改条目（food_id 来自 list_foods）。只传需要改的字段，空串/0 表示不改。"""
    fields: dict[str, Any] = {}
    if name:
        fields["name"] = name.strip()
    if category:
        fields["category"] = category
    if quantity > 0:
        fields["quantity"] = quantity
    if production_date:
        fields["production_date"] = production_date
    if shelf_life_days > 0:
        fields["shelf_life_days"] = shelf_life_days
    if not fields:
        return "没有需要修改的字段"
    row = api("PATCH", f"/foods/{food_id}", fields)
    return f"已修改：{summarize([row], include_eaten=True)}"


@app.add_tool
def consume_food(food_id: int) -> str:
    """标记条目为已吃完（软删除，可用 restore_food 恢复）。"""
    row = api("DELETE", f"/foods/{food_id}")
    return f"已标记吃完：{summarize([row], include_eaten=True)}"


@app.add_tool
def restore_food(food_id: int) -> str:
    """把已吃完的条目恢复为在库。"""
    row = api("POST", f"/foods/{food_id}/restore")
    return f"已恢复：{summarize([row], include_eaten=True)}"


@app.add_tool
def delete_food(food_id: int) -> str:
    """彻底删除条目（不可恢复）。一般用 consume_food 即可。"""
    api("DELETE", f"/foods/{food_id}?hard=true")
    return f"已删除条目 #{food_id}"


def main() -> None:
    app.run("stdio")


if __name__ == "__main__":
    main()