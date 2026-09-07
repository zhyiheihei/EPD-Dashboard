# EPD-Dashboard · 家庭食品存储看板

家庭食品存储管理系统：**客户端**（桌面 App，无边框 UI）管理食品数据 → **服务端**（跑在 opi5p
家庭服务器上）落库 PostgreSQL、在每天 0 点把"最近到期 Top4"推送到墨水屏。

```
┌────────────────┐  HTTPS + Token(内网)   ┌──────────────────────┐  BLE 看板协议 v1   ┌───────────────┐
│ 客户端 (桌面)    │ ────────────────────→ │ 服务端 (opi5p)        │ ────────────────→ │ EPD-nRF5 墨水屏 │
│ Tauri2 + Vue3  │  /api/foods CRUD      │ FastAPI + PostgreSQL  │ 1bit 位图+到期数据  │ 800×480 BWR    │
│ 无边框窗口      │  /api/epd/push        │ + bleak + Pillow 渲染  │                   └───────────────┘
└────────────────┘                       └──────────────────────┘
```

设计约束（来自目标机器固件）：

- 墨水屏设备**不接收文本**（无法存储字库）：服务端用 Pillow 把食品名称渲染成
  1-bit 位图（152×20，MSB-first，1=黑），随到期时间经 BLE 事务下发；
- 单屏最多 **4 条食品 + 2 条日程**，服务端按"最近到期优先"取 Top4；
- 协议详见 `../EPD-nRF5/docs/dashboard-protocol-v1.md`（帧构造在本仓库
  `server/epd_food_server/protocol.py`，与固件参考实现 `html/js/main.js` 逐字节对齐）；
- 客户端**只**与服务端通信（HTTPS + Bearer Token），不直接触碰数据库与蓝牙。

## 目录

| 路径 | 内容 |
| --- | --- |
| `server/` | Python 服务端（FastAPI REST API + bleak BLE 客户端 + Pillow 文字渲染） |
| `client/` | Tauri 2 + Vue 3 + Element Plus 客户端（无边框窗口） |
| `deploy/food-storage.nix` | NixOS 模块（PostgreSQL + systemd 服务/0 点定时器 + nginx vhost） |
| `tests/` | 协议帧 / 渲染 / API 测试 |
| `docs/` | 架构文档与部署手册 |

## 快速开始

### 服务端（本机开发）

```bash
nix develop                      # 进入带全部依赖的 shell
cd server
export EPD_FOOD_DSN="host=/run/postgresql dbname=epd_dashboard"  # 或 sqlite 不支持，需 PG
export EPD_FOOD_API_TOKEN="dev-token"
epd-food-server serve            # 或 python -m epd_food_server serve
```

首次启动自动建表。手动立即推送墨水屏：`epd-food-server push-now`。

### 客户端

```bash
nix develop .#client -c bash -c 'cd client && npm install && npm run tauri dev'
```

### 部署到 opi5p

见 [docs/deploy.md](docs/deploy.md)。

## 运行测试

```bash
nix develop -c pytest tests/ -v
```
