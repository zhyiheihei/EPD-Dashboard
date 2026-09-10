# EPD-Dashboard 部署与架构要点

家庭食品存储看板：PostgreSQL 存食品条目 → FastAPI 服务端渲染位图 → BLE（NRF52811）推送到 7.5 寸三色墨水屏（UC8179）。

## 入口

- REST API + WebUI：`/api/*` 与 `/ui/`（微信风格 SPA，静态托管）
- MCP server：`epd_dashboard_mcp`（6 工具，见 server/epd_dashboard_mcp.py）
- systemd timer：每日 0 点推送墨水屏

## 仓库与部署

| 仓库 | 路径 | 说明 |
| --- | --- | --- |
| 本仓库 | `~/Documents/repositories/EPD-Dashboard` | 服务端 + WebUI + MCP（Python，main 推 GitHub） |
| EPD-nRF5 | `~/Documents/repositories/EPD-nRF5` | 固件（C） |
| zhyi-packages | `~/Documents/nixos/zhyi-packages` | nvfetcher 源 pin + `pkgs/uncategorized/epd-food-server` + `nixos-modules/food-dashboard.nix` |
| nixos-config | `~/Documents/nixos/nixos-config` | 接入壳 `nixos/optional-apps/food-dashboard.nix`，主机配置在 `hosts/opi5p/configuration.nix` |

- 生产：opi5p `ssh -p 2222 root@192.168.0.62`，服务 `epd-food-server.service`（127.0.0.1:13836）+ `epd-food-push.timer`
- 数据库：PostgreSQL 库/角色统一 `epd_dashboard`（下划线）
- 部署：`cd ~/Documents/nixos/nixos-config && nix run .#colmena -- apply --on opi5p`

### 源码 pin 更新流程（zhyi-packages）

EPD-Dashboard push 到 GitHub → zhyi-packages `nvfetcher -f epd-food-server`（网络慢易超时；可 nix-prefetch-git 拿 rev+sha256 手动改 `_sources/generated.nix`，`nix hash to-sri --type sha256` 转 SRI）→ commit+push → nixos-config `nix flake lock --update-input zhyi-packages` → colmena 部署。nixos-config 的 input 是 GitHub pin，只改本地不更新 lock 不生效。

## 日程栏（CalDAV 只读）

标准 RFC 4791 实现（`server/epd_food_server/caldav.py`）：PROPFIND 日历发现 → REPORT calendar-query（time-range）→ 极简 ICS 解析（UTC Z / TZID / VALUE=DATE，DAILY/WEEKLY RRULE 基础展开），纯标准库。

- 生产日历：`https://cal.zhyi.xin`（Radicale），根路径即服务根——日历在 `/zhyi/calendar/`，无 `/dav` 前缀
- 配置：`EPD_FOOD_CALDAV_URL/USER/PASSWORD/CALENDAR` + `SCHEDULE_DAYS`；密码用全局 sops secret `default-pw`
- 协议：日程标题位图 320×20（槽 0x00/0x01），BEGIN 帧带 ScheduleRecord（每条 10B）；食品名称 152×20（槽 0x10–0x13）
- 推送策略：拉日程失败降级为无日程不阻塞；日程位图指纹存 `state_dir/last-schedules.txt`，变化时强制全刷（局部刷新只刷食品面板区）

## 操作红线（血泪教训）

- bleak 必须 `<1`（现 0.22.3）；同一时刻只允许一个 BLE 扫描进程
- 判断推送是否生效必须看屏幕实际内容，不能只信日志（v25 固件曾协议层全成功但屏幕不动）
- 生产验证前 `ss -tlnp` 确认端口归属、psql 直查生产库对数（曾发生本地假隧道事故）
- `pkill -f "epd_food_server"` 会自杀，用 `[e]pd_food_server` 模式
-opi5p 蓝牙若被 rfkill 软屏蔽：`rfkill unblock bluetooth`（固化进 nixos 配置尚未做）
- 固件 OTA：改 `APP_VERSION` → `make -f Makefile.firmware clean && make -f Makefile.firmware ota`（nix develop 内）→ 服务端 API 上传推送

## 测试

`pytest tests/`（本仓库根目录），当前 72 例。测试环境 PostgreSQL `/tmp/epd-pgt4`（port 15434）、测试服务端 port 18313 token `e2e-token`。