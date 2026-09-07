# 架构文档

## 组件与职责

| 组件 | 运行位置 | 职责 | 绝不做的事 |
| --- | --- | --- | --- |
| 客户端 `client/` | 用户桌面（Tauri 2 + Vue 3） | 食品 CRUD 界面、推送触发、状态展示 | 不碰数据库、不碰蓝牙 |
| 服务端 `server/` | opi5p（NixOS） | REST API、PostgreSQL 存储、文字栅格化、BLE 推送、0 点定时 | 不暴露除 `/api/*` 以外的能力 |
| 墨水屏固件 | EPD-nRF5 (nRF52811) | 本地绘制日历/边框/图标/剩余天数，接收 1-bit 位图与到期元数据 | — |

客户端 ⇄ 服务端：唯一入口，HTTPS（nginx vhost，仅私网）+ Bearer Token。
服务端 ⇄ 设备：BLE Central（服务端），设备为广播外设，无配对加密。

## 数据模型

```
foods(id, name, category, production_date, shelf_life_days, quantity,
      created_at, updated_at, consumed_at)
```

- `expiry_date = production_date + shelf_life_days`（服务端计算）
- `consumed_at` 软删除：吃掉/清理的食品不参与上屏
- 品类型 `category` 是自由文本（客户端提供预设），映射到设备协议：
  `category == "饮品"` → `type=1`，其余 → `type=0`

## 上屏选择策略

```
SELECT * FROM foods
WHERE consumed_at IS NULL
ORDER BY (production_date + shelf_life_days) ASC, created_at DESC
LIMIT 4;
```

到期时间下发为 `expiry_date 当天 23:59:59（本地时区）` 的 UTC 秒数
（与固件网页参考实现一致）。库存为空时发送 0 条食品，设备清空食品面板。

## BLE 会话（看板协议 v1，固件 v0x1F）

```
扫描(service UUID 62750001-…, 名称前缀 NRF_EPD)
 → 连接 + 订阅 Notify（0x62750002-…）
 → INIT(0x01)                    ← 设备回文本 mtu=N，确定分片大小
 → CAPS(0x40)                    ← 校验协议版本=01、800×480、功能位
 → BEGIN(0x41)                   ← UTC+时区(+480)+周起始+食品记录[slot,type,expiry_utc]
 → BITMAP(0x42) × N              ← 每条食品名称一张 152×20 位图，槽位 0x10+i
                                    分片写（ATT write with response），末片带 CRC-16/CCITT-FALSE
                                    非末片无 C0 应答，末片等 42 OK；逐资源串行
 → COMMIT(0x43 flags=03)         ← 刷新 + 刷新后休眠驱动；等 43 OK 后再等屏幕物理刷新完成
失败路径：ABORT(0x44) + 换事务 ID 重试（指数退避，共 2 次重试）
```

分片大小：`max_write = MTU - 3`；单片数据 ≤ `max_write - 14`（12 字节头 + 2 字节 CRC）。
MTU 244 时单片约 230 字节（Linux/BlueZ 默认可用）；可配置 `EPD_FOOD_MAX_CHUNK=6`
强制走与 Windows 网页端相同的保守分片。

并发互斥：API 触发与 0 点 systemd timer 触发共用 `/var/lib/epd-dashboard/push.lock`
（flock），同一时刻只允许一个 BLE 会话。

## 服务端模块图

```
epd_food_server/
├── main.py      CLI（serve / push-now）+ FastAPI app 工厂
├── config.py    环境变量配置（EPD_FOOD_*）
├── db.py        psycopg3（unix socket 对等认证），启动自动建表
├── models.py    Pydantic 模型（入参校验 + 出参序列化）
├── render.py    Pillow：文本 → 1-bit 位图（18px 起步缩放 → 省略号截断）
├── protocol.py  看板协议 v1 纯函数（帧构造/解析/CRC），可独立单测
├── ble.py       bleak 会话：扫描/连接/通知路由/事务流程
├── pusher.py    推送编排：Top4 选择、文件锁、防抖、变更节流（30 分钟）
├── static/      WebUI 单页应用（挂 /ui，/ 重定向；原型 docs/ui/prototype-final.html）
└── api/
    ├── foods.py /api/foods CRUD + stats + meta
    ├── epd.py   /api/epd push/status/preview(.png) + /api/health
    └── ota.py   /api/ota firmware 上传/列表/删除、设备版本、升级
```

### WebUI

浏览器直接访问 `http://服务端:端口/`（重定向到 `/ui/`）。四页：库存（微信风列表，
左滑/甩动吃完、点击菜单、记一笔、已吃完折叠分组、FLIP 动画）、墨水屏（真实布局
预览 + 推送状态 + 手动刷新）、固件（OTA 管理）、设置。鉴权：服务端配置了
API_TOKEN 时首次访问需在页内顶栏输入令牌（存 localStorage）。端到端回归：
`docs/ui/webtest.py`（需服务运行中）。

## 关键环境变量（NixOS 模块注入）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EPD_FOOD_DSN` | — | psycopg DSN， opi5p 上为 `host=/run/postgresql dbname=epd_dashboard` |
| `EPD_FOOD_API_TOKEN` | 空=关闭鉴权 | 客户端 Bearer Token |
| `EPD_FOOD_FONT_PATH` | 自动探测 | CJK 字体文件（模块指向 Noto Sans CJK Bold） |
| `EPD_FOOD_DEVICE_NAME_PREFIX` | `NRF_EPD` | BLE 设备名前缀过滤 |
| `EPD_FOOD_MAX_CHUNK` | 0=按 MTU 自动 | 单片数据字节上限（调试用 6） |
| `EPD_FOOD_PUSH_ON_CHANGE` | true | 数据变更后防抖即时推送 |
| `EPD_FOOD_PUSH_DEBOUNCE` | 10 秒 | 合并连续写入 |
| `EPD_FOOD_FULL_REFRESH_EVERY` | 8 | 每 N 次推送强制全刷清残影；0 = 始终全刷 |
| `EPD_FOOD_CHANGE_MIN_INTERVAL` | 1800 秒 | 条目变更推送最小间隔（节流），变更留待 0 点或下次变更一并上屏 |
| `EPD_FOOD_COMMIT_SLEEP` | true | COMMIT 是否带休眠标志（调试局部刷新用） |
| `EPD_FOOD_STATE_DIR` | `/var/lib/epd-dashboard` | 锁与推送状态文件目录 |

全部变量见 `server/epd_food_server/config.py`。

## 推送触发点

1. **每天 0:00** systemd timer `epd-food-push.timer`（Persistent=true，错过后补跑）
2. **数据变更**（增/删/改/恢复）防抖 10s 合并后推送（`EPD_FOOD_PUSH_ON_CHANGE`）
3. **手动**：客户端推送页按钮 → `POST /api/epd/push`；或服务器上 `epd-food-server push-now`

设备断电/重启后位图丢失（协议 RAM-only），以上任一触发即可重绘。
