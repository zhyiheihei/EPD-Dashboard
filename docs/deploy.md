# 部署手册（opi5p）

## 前置确认（一次性）

```bash
ssh opi5p
bluetoothctl show          # 确认适配器 Powered: yes；如未开机：
                           # bluetoothctl power on（NixOS 已 hardware.bluetooth.enable）
systemctl status postgresql
```

## 1. secrets（nixos-secrets 仓库）

在 `nixos-secrets` 中新增条目（文件与 key 名按你现有 sops 规则放置），例如
`common/epd-dashboard.yaml`：

```yaml
epd-dashboard:
    api-token: <openssl rand -hex 32 生成>
```

`.sops.yaml` 的 creation_rules 若按路径匹配，确认该文件会被 opi5p 的 age key 加密。

## 2. nixos-config 接入

```nix
# flake.nix inputs 增加：
epd-dashboard.url = "github:<you>/EPD-Dashboard";   # 本地开发可用 path:/home/zhyi/Documents/repositories/EPD-Dashboard

# hosts/opi5p/configuration.nix：
imports = [ inputs.epd-dashboard.nixosModules.food-storage ];

lantian.food-dashboard = {
  enable = true;
  tokenFile = config.sops.secrets."epd-dashboard/api-token".path;
};

# sops 声明（与仓库其他 secret 同风格）：
sops.secrets."epd-dashboard/api-token" = {
  sopsFile = inputs.secrets + "/common/epd-dashboard.yaml";
  owner = "epd-dashboard";
  group = "epd-dashboard";
};
```

可选：`port`（默认 8386，建议在 `helpers/constants/ports.nix` 登记
`EpdFoodDashboard = 8386;`）、`deviceNamePrefix`、`pushOnChange`、`maxChunk`。

## 3. 部署

```bash
# 控制机（ml-laptop）
nix run .#colmena -- apply --on opi5p
```

## 4. 验证

```bash
ssh opi5p
systemctl status epd-food-server          # API 常驻
systemctl list-timers epd-food-push       # 0 点定时器
journalctl -u epd-food-push -f            # 推送日志

# 手动推送一次（墨水屏在附近时）
systemctl start epd-food-push

curl -s https://food.opi5p.zhyi.xin/api/health -H "Authorization: Bearer $(cat token)"  # 经 nginx
curl -s 127.0.0.1:8386/api/health                                                        # 本机直连（无鉴权头时 401 属正常）
```

## 故障排查

| 现象 | 排查 |
| --- | --- |
| `未找到墨水屏设备` | `bluetoothctl devices` 看广播；`bluetoothctl scan on` 确认 `NRF_EPD_XXXX` 可见；设备上电、在 10m 内 |
| 推送超时 | `EPD_FOOD_MAX_CHUNK=6` 走保守分片试试（与网页端一致）；缩短设备距离 |
| 推送成功但屏幕没变 | 全刷约 15s、局部刷新仅刷新食品栏约 2s；`43 OK` 后服务端已按 settle 等待 |
| 局部刷新后屏幕周围轻微灰边 | 窗口边界电容串扰（物理特性）；每次推送局部刷新，每 8 次强制全刷 + 每日午夜全刷自动清除 |
| BLE 失败、D-Bus 权限 | `sudo -u epd-dashboard dbus-send --system --print-reply --dest=org.bluez / org.freedesktop.DBus.Introspectable.Introspect` |
| 字体方块 | `ls -l /nix/store/*epd-food-cjk-font*`；journal 里 EPD_FOOD_FONT_PATH 是否存在 |
| 数据库权限 | ensureUsers 已建 `epd-dashboard` 角色；`sudo -u postgres psql -c '\l'` 看 epd_dashboard 属主 |

## 设备端注意事项（来自协议文档）

- 设备重启后位图丢失（RAM-only），等 0 点 timer 或手动 `systemctl start epd-food-push` 即可重绘
- 链路无加密无认证：仅在内网使用，勿经公网暴露
- v1F 固件已移除旧的全屏图片/清屏等命令，服务端只用看板协议 v1 五条命令
