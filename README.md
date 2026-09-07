# EPD-Dashboard · 家庭食品存储看板

家庭食品存储管理系统：**客户端**（桌面 App，无边框 UI）或 **WebUI**（浏览器直接
访问）管理食品数据 → **服务端**（跑在 opi5p 家庭服务器上）落库 PostgreSQL、把
"最近到期 Top4"推送到墨水屏：数据变更防抖即时推送（局部刷新），每天首次推送
全刷（更新日期/倒计时、清残影）+ 0 点定时兜底。

```
┌────────────────┐  HTTPS + Token(内网)   ┌──────────────────────┐  BLE 看板协议 v1   ┌───────────────┐
│ 客户端 (桌面)    │ ────────────────────→ │ 服务端 (opi5p)        │ ────────────────→ │ EPD-nRF5 墨水屏 │
│ Tauri2 + Vue3  │  /api/foods CRUD      │ FastAPI + PostgreSQL  │ 1bit 位图+到期数据  │ 800×480 BWR    │
│ 无边框窗口      │  /api/epd/push        │ + bleak + Pillow 渲染  │                   └───────────────┘
└────────────────┘                       └──────────────────────┘

同一套 REST API 还有两个入口：**WebUI**（浏览器访问服务端 `/ui`，开箱即用）与
**MCP**（stdio 服务端，供 AI Agent 管理库存）。服务端另经 BLE Secure DFU
对墨水屏做固件 OTA 升级。
```

三个入口（桌面客户端 / WebUI / MCP）共用同一套 REST API 与 Bearer Token 鉴权。

设计约束（来自目标机器固件）：

- 墨水屏设备**不接收文本**（无法存储字库）：服务端用 Pillow 把食品名称渲染成
  1-bit 位图（152×20，MSB-first，1=黑），随到期时间经 BLE 事务下发；
- 单屏最多 **4 条食品 + 2 条日程**，服务端按"最近到期优先"取 Top4；
- 墨水屏刷新寿命有限：变更推送经防抖（10s）+ 节流（30 分钟）合并；平时局部刷新
  食品栏（约 2s 不闪屏），每天首次推送才全刷（固件 v24 起不再午夜自刷）；
- 协议详见 `../EPD-nRF5/docs/dashboard-protocol-v1.md`（帧构造在本仓库
  `server/epd_food_server/protocol.py`，与固件参考实现 `html/js/main.js` 逐字节对齐）；
- 客户端/WebUI/MCP **只**与服务端通信（HTTPS + Bearer Token），不直接触碰数据库
  与蓝牙。

## 目录

| 路径 | 内容 |
| --- | --- |
| `server/` | Python 服务端（FastAPI REST API + bleak BLE 客户端 + Pillow 文字渲染 + Secure DFU OTA） |
| `server/epd_dashboard_mcp.py` | MCP 服务端（stdio）：让 AI Agent 直接管理食品库存 |
| `client/` | Tauri 2 + Vue 3 + Element Plus 客户端（无边框窗口，五页含 OTA 管理） |
| `deploy/food-storage.nix` | NixOS 模块（PostgreSQL + systemd 服务/0 点定时器 + nginx vhost） |
| `tests/` | 协议帧 / 渲染 / API / 局部刷新策略 / OTA 包 / DFU 引擎测试 |
| `docs/` | 架构文档、部署手册、WebUI 原型与端到端回归脚本 |

## 快速开始

### 服务端（本机开发）

```bash
nix develop                      # 进入带全部依赖的 shell（含字体与 PG；DSN 已预设）
cd server
export EPD_FOOD_DSN="host=/run/postgresql dbname=epd_dashboard"  # 按本机 PG 调整
export EPD_FOOD_API_TOKEN="dev-token"
epd-food-server serve            # 或 python -m epd_food_server serve [--host --port]
```

首次启动自动建表（含渐进迁移）。浏览器打开 `http://127.0.0.1:8386/` 即是 WebUI
（库存管理、墨水屏预览与手动推送、固件 OTA、设置）。其他命令：

```bash
epd-food-server push-now               # 手动立即推送墨水屏（0 点 timer 也走这条）
epd-food-server ota-push fw-ota.zip    # 上传 nrfutil OTA 包并对设备升级（约 90s）
```

### 桌面客户端

```bash
nix develop .#client -c bash -c 'cd client && npm install && npm run tauri dev'
```

### AI Agent（MCP）

`server/epd_dashboard_mcp.py` 是 stdio MCP 服务端（依赖官方 `mcp` 包，仓库 `.venv`
已装），提供 list/add/update/consume/restore/delete 六个库存工具；录入与修改后
服务端自动防抖推送墨水屏，agent 无需关心推送。宿主（Claude Desktop 等）配置：

```json
{
  "mcpServers": {
    "epd-dashboard": {
      "command": "/path/to/EPD-Dashboard/.venv/bin/python",
      "args": ["-m", "epd_dashboard_mcp"],
      "cwd": "/path/to/EPD-Dashboard/server",
      "env": { "EPD_MCP_BASE_URL": "http://127.0.0.1:8386",
               "EPD_MCP_TOKEN": "dev-token" }
    }
  }
}
```

服务端开启鉴权时 `EPD_MCP_TOKEN` 必填；`EPD_MCP_BASE_URL` 缺省
`http://127.0.0.1:18313`，指向实际服务端地址即可。

### 部署到 opi5p

见 [docs/deploy.md](docs/deploy.md)。

## 运行测试

```bash
nix develop -c pytest tests/ -v
```

覆盖：协议帧构造/解析、文字渲染、REST API（FakeDB + httpx）、局部/全刷推送策略、
OTA 包解析、Secure DFU 引擎（仿真 bootloader）。WebUI 另有端到端回归
`docs/ui/webtest.py`（需服务运行中）。
