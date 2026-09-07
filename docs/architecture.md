# 架构文档

## 组件与职责

| 组件 | 运行位置 | 职责 | 绝不做的事 |
| --- | --- | --- | --- |
| 桌面客户端 `client/` | 用户桌面（Tauri 2 + Vue 3） | 食品 CRUD 界面、推送触发、状态展示、固件 OTA 管理 | 不碰数据库、不碰蓝牙 |
| WebUI `server/epd_food_server/static/` | 浏览器（由服务端挂 `/ui`） | 与客户端同等覆盖：库存管理、墨水屏预览/手动刷新、OTA、设置展示 | 不碰数据库、不碰蓝牙（全走 REST API） |
| MCP 服务端 `server/epd_dashboard_mcp.py` | 本地 AI Agent 宿主（stdio） | 食品库存管理工具集（列表/增/改/吃完/恢复/删），薄封装 REST API | 不碰数据库、不碰蓝牙、不做 OTA |
| 服务端 `server/` | opi5p（NixOS） | REST API、PostgreSQL 存储、文字栅格化、BLE 推送、固件 OTA、0 点定时 | 不暴露除 `/api/*` 以外的能力 |
| 墨水屏固件 | EPD-nRF5 (nRF52811) | 本地绘制日历/边框/图标/剩余天数，接收 1-bit 位图与到期元数据，Secure DFU bootloader | — |

桌面客户端、WebUI、MCP 三个入口全部走同一个 REST API：HTTPS（nginx vhost，仅私网）
+ Bearer Token。服务端 ⇄ 设备：BLE Central（服务端），设备为广播外设，无配对加密。

## 数据模型

```
foods(id, name, category, production_date, shelf_life_days, quantity,
      created_at, updated_at, consumed_at, interaction_count)

firmware_packages(id, filename, app_version, device_type, softdevice_required,
                  size, sha256 UNIQUE, stored_path, uploaded_at)
```

- `expiry_date = production_date + shelf_life_days`（服务端计算；DB 上有
  `WHERE consumed_at IS NULL` 的部分索引加速 Top4 查询）
- `consumed_at` 软删除：吃掉/清理的食品不上屏，可恢复；`interaction_count`
  在每次修改/吃完/恢复时 +1，WebUI 按交互频率排序
- 品类型 `category` 是自由文本（客户端提供预设），映射到设备协议：
  `category == "饮品"` → `type=1`，其余 → `type=0`
- schema 变更用 `ADD COLUMN IF NOT EXISTS` 渐进迁移，服务启动时自动执行
- OTA 包按 sha256 幂等入库（重复上传同一包返回已有行），文件落 `state_dir/ota/`

## 上屏选择策略

```
SELECT * FROM foods
WHERE consumed_at IS NULL
ORDER BY (production_date + shelf_life_days) ASC, created_at DESC
LIMIT 4;
```

到期时间下发为 `expiry_date 当天 23:59:59（本地时区）` 的 UTC 秒数
（与固件网页参考实现一致）。库存为空时发送 0 条食品，设备清空食品面板。

## BLE 会话（看板协议 v1）

```
扫描（服务 UUID 62750001-…，名称前缀 NRF_EPD 或指定地址）
 → 连接 + 订阅 Notify（0x62750002-…）
    连接返回后轮询等待 GATT 服务解析完成（防 BlueZ "Service Discovery
    has not been performed yet" 竞态，最长 5s）
 → INIT(0x01)                    ← 设备回文本 mtu=N，确定分片大小；超时回退 bleak MTU（下限 23）
 → CAPS(0x40)                    ← 校验协议版本=01、800×480、功能位
 → BEGIN(0x41)                   ← UTC+时区(+480)+周起始+食品记录[slot,type,expiry_utc]
 → BITMAP(0x42) × N              ← 每条食品名称一张 152×20 位图，槽位 0x10+i
                                    分片写（ATT write with response），末片带 CRC-16/CCITT-FALSE
                                    非末片无 C0 应答，末片等 42 OK；逐资源串行
 → COMMIT(0x43)                  ← flags 由服务端决定（见下节刷新策略）；等 43 OK 后
                                    按 settle 时间等屏幕物理刷新完成
失败路径：整个会话换事务 ID 重试（退避 retry_backoff×尝试次数，默认 12s 起，
共 push_retries 次）；单片写入失败短暂重试 3 次（0.3s 间隔）
```

分片大小：`max_write = MTU - 3`；单片数据 ≤ `max_write - 14`（12 字节头 + 2 字节 CRC）。
MTU 244 时单片约 230 字节（Linux/BlueZ 默认可用）；可配置 `EPD_FOOD_MAX_CHUNK=6`
强制走与 Windows 网页端相同的保守分片。

固件版本读取：版本特征 `0x62750003`（1 字节）。`GET /api/ota/device` 默认走
5 分钟 TTL 缓存（`refresh=1` 才真正连接设备）——设备不干活时应保持休眠，
避免每次开页面都隐式唤醒。

## 刷新策略（全刷 vs 局部）

固件 v24 起移除了午夜自动全刷（会把服务端位图盖掉），日期/倒计时更新与残影
清理全部由服务端负责：

- **当天首次推送强制全刷**：上次全刷日期持久化在 `state_dir/last-full-refresh.txt`，
  服务端重启不重置；
- 其余推送走**局部刷新**（COMMIT flag `0x04`，需 CAPS 特征位 `1<<6`）：
  仅刷新食品面板内容区，约 2s、不闪屏；
- 每 `EPD_FOOD_FULL_REFRESH_EVERY`（默认 8）次推送额外全刷一次清残影，
  0 = 关闭（已有每日全刷兜底）；
- COMMIT OK 后 settle 等待：全刷 16s（`EPD_FOOD_SETTLE_SECONDS`）、局部 3s，
  等完才释放文件锁。

## OTA 固件升级（Nordic Secure DFU）

- 包格式：nrfutil 6 生成的 OTA zip（manifest.json + application bin/dat）。
  版本从 init 包 protobuf 解析，解析不出时回退文件名约定 `*-v<HEX>-ota.zip`；
  上限 20MB；文件落 `state_dir/ota/`，DB 按 sha256 幂等。
- 入口：WebUI 固件页拖入上传、点"升级"；或 CLI `epd-food-server ota-push <zip>`
  （包先入库再升级）；API `POST /api/ota/push` 异步执行，`GET /api/ota/status`
  轮询进度。
- 流程：应用模式向 Buttonless 特征（`8EC90003`，FE59 服务）写 `0x01` → 设备复位
  进 bootloader、以 DfuTarg 重新广播 → Secure DFU 控制点/数据点（`8EC90001/2`）
  按对象写 init dat + 应用 bin，CRC32 校验逐对象 execute → 完成后自动重启回应用
  模式。全程约 90 秒。
- 失败恢复：升级失败设备停留在 bootloader（DfuTarg 广播），直接再次发起升级
  即可，无需额外抢救。
- 并发：OTA 用独立 `ota.lock`（与推送锁 `push.lock` 相互独立）。

## 服务端模块图

```
epd_food_server/
├── main.py      CLI（serve / push-now / ota-push）+ FastAPI app 工厂
├── config.py    环境变量配置（EPD_FOOD_*）
├── db.py        psycopg3（unix socket 对等认证），启动自动建表 + 渐进迁移
├── models.py    Pydantic 模型（入参校验 + 出参序列化）
├── render.py    Pillow：文本 → 1-bit 位图（18px 起步缩放 → 省略号截断，
│                可变字体强制 wght=700 加粗，ttc 优先 SC 子字体）
├── protocol.py  看板协议 v1 纯函数（帧构造/解析/CRC），可独立单测
├── ble.py       bleak 会话：扫描/连接/通知路由/看板事务 + 固件版本读取（带缓存）
├── dfu.py       Nordic Secure DFU 客户端（buttonless 进 bootloader → init/data 对象传输）
├── ota.py       OTA 包解析（zip + init 包 protobuf 最小 wire 解析）、存储、升级编排
├── pusher.py    推送编排：Top4 选择、文件锁、防抖/节流、全刷/局部刷新策略
├── static/      WebUI 单页应用（单文件 index.html，挂 /ui，/ 重定向；
│                原型备份 docs/ui/prototype-final.html）
└── api/
    ├── foods.py /api/foods CRUD + stats
    ├── epd.py   /api/epd push/status/preview(.png) + /api/health
    ├── ota.py   /api/ota firmware 上传/列表/删除、设备版本、升级、状态
    └── deps.py  路由共享小工具（notify_change 变更通知，线程安全）

epd_dashboard_mcp.py   MCP 服务端（stdio，包外单文件）：AI Agent 食品管理工具
```

### REST API 一览

| 端点 | 说明 |
| --- | --- |
| `GET/POST /api/foods`、`GET/PATCH/DELETE /api/foods/{id}` | 食品 CRUD；DELETE 默认软删（吃完），`?hard=true` 物理删 |
| `POST /api/foods/{id}/restore` | 恢复已吃完条目 |
| `GET /api/foods/stats` | 统计（在库/已过期/3 天内/7 天内/已吃完/按品类） |
| `POST /api/epd/push` | 手动推送（异步，立即返回；`GET /api/epd/status` 轮询） |
| `GET /api/epd/preview`、`/api/epd/preview.png` | Top4 预览（JSON / 800×480 整屏 PNG） |
| `GET /api/health`、`GET /api/meta` | 健康检查（含 DB）；品类预设、字体可用性 |
| `POST/GET /api/ota/firmware`、`DELETE /api/ota/firmware/{id}` | 固件包上传（原始二进制，文件名走 query）/列表/删除 |
| `GET /api/ota/device?refresh=` | 当前设备固件版本（默认 5 分钟缓存） |
| `POST /api/ota/push?firmware_id=`、`GET /api/ota/status` | 触发升级（缺省最新包）/进度与结果 |

列表支持 `status_filter=active|consumed|all` 与 `order=expiry|created|name`。

### WebUI

浏览器直接访问 `http://服务端:端口/`（重定向到 `/ui/`），单文件 `index.html`，
微信风格四页：

- **库存（首页）**：左滑/甩动吃完、点击行出菜单、右下角"记一笔"底部弹层
  （品类 食品/饮品、数量步进、生产日期、保质期 月/日/单位切换）、已吃完折叠
  分组、按交互频率排序、增删改全覆盖 FLIP 动画；
- **墨水屏**：整屏真实布局预览（`preview.png`）、手动推送 + 状态轮询、设备卡
  （固件版本走服务端 5 分钟缓存，"刷新"链接才真正连设备）；
- **固件**：当前设备版本、拖拽上传 OTA zip、固件包列表、升级进度条（约 90s）；
- **设置**：服务连接与推送策略展示（展示性页面，改配置走 NixOS 模块）。

鉴权：服务端配置了 API_TOKEN 时首次访问需在页内顶栏输入令牌（存 localStorage，
之后随 API 请求带 Bearer 头）。静态文件带 `Cache-Control: no-cache`，改完刷新即生效。
端到端回归：`docs/ui/webtest.py`（需服务运行在 127.0.0.1:18313，Chrome/CDP）。

### MCP 服务端（AI Agent 入口）

`server/epd_dashboard_mcp.py`：stdio 传输，依赖官方 `mcp` Python 包（仓库 `.venv`
已装，nix devShell 未含）。只封装食品 CRUD 六个工具
（`list_foods` / `add_food` / `update_food` / `consume_food` / `restore_food` /
`delete_food`）——录入与修改后服务端自动防抖推送墨水屏，agent 无需关心推送。
环境变量：`EPD_MCP_BASE_URL`（默认 `http://127.0.0.1:18313`）、
`EPD_MCP_TOKEN`（服务端开启鉴权时必填）。宿主配置示例见文件头部 docstring。

## 关键环境变量（NixOS 模块注入）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EPD_FOOD_DSN` | `host=/run/postgresql dbname=epd_dashboard` | psycopg DSN |
| `EPD_FOOD_API_TOKEN` | 空=关闭鉴权 | 客户端/WebUI/MCP 共用的 Bearer Token |
| `EPD_FOOD_BIND_HOST` / `BIND_PORT` | `127.0.0.1` / `8386` | API 监听地址（CLI `--host/--port` 可覆盖） |
| `EPD_FOOD_TIMEZONE` | `Asia/Shanghai` | 到期时间换算时区 |
| `EPD_FOOD_FONT_PATH` | 自动探测 | CJK 字体文件（模块指向 Noto Sans CJK Bold；探测含 fc-match） |
| `EPD_FOOD_DEVICE_NAME_PREFIX` | `NRF_EPD` | BLE 设备名前缀过滤 |
| `EPD_FOOD_DEVICE_ADDRESS` | — | 固定设备地址（设置后跳过名称匹配） |
| `EPD_FOOD_MAX_CHUNK` | 0=按 MTU 自动 | 单片数据字节上限（调试用 6） |
| `EPD_FOOD_PUSH_ON_CHANGE` | true | 数据变更后防抖即时推送 |
| `EPD_FOOD_PUSH_DEBOUNCE` | 10 秒 | 合并连续写入 |
| `EPD_FOOD_CHANGE_MIN_INTERVAL` | 1800 秒 | 变更推送最小间隔（节流）；节流期内的变更留待 0 点或下次变更一并上屏 |
| `EPD_FOOD_FULL_REFRESH_EVERY` | 8 | 每 N 次推送强制全刷清残影；0 = 关闭（当天首推必全刷） |
| `EPD_FOOD_SETTLE_SECONDS` | 16 | COMMIT OK 后等屏幕物理刷新的时间（局部刷新固定 3s） |
| `EPD_FOOD_PUSH_RETRIES` / `RETRY_BACKOFF` | 2 / 12 秒 | 推送失败重试次数与退避基数（设备失败后需 ~10s 重新广播，勿设太短） |
| `EPD_FOOD_COMMIT_SLEEP` | true | COMMIT 是否带休眠标志（调试局部刷新用） |
| `EPD_FOOD_STATE_DIR` | `/var/lib/epd-dashboard` | 锁、状态文件、OTA 包目录（非 root 开发时 `~/.local/state/epd-food-server`） |

全部变量见 `server/epd_food_server/config.py`。

### state_dir 布局

```
state_dir/
├── push.lock / ota.lock      flock 互斥（推送 90s、OTA 90s 等待上限）
├── push-status.json          最近一次推送结果（/api/epd/status 数据源）
├── ota-status.json           最近一次 OTA 结果与字节进度
├── last-full-refresh.txt     上次全刷日期（当天首推全刷的判定依据）
└── ota/                      固件包落盘（文件名带 sha256 前缀）
```

## 推送触发点

1. **每天 0:00** systemd timer `epd-food-push.timer`（Persistent=true，错过后补跑；
   通常即当天首次推送 → 全刷）
2. **数据变更**（增/删/改/恢复）：防抖 10s 合并后推送（`EPD_FOOD_PUSH_ON_CHANGE`）；
   距上次成功推送不足 30 分钟则节流留待下次；已有推送进行中则直接跳过
   （那次推送已带最新数据）
3. **手动**：客户端/WebUI 墨水屏页按钮 → `POST /api/epd/push`；
   或服务器上 `epd-food-server push-now`

设备断电/重启后位图丢失（协议 RAM-only），以上任一触发即可重绘。
