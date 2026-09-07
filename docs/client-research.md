# 客户端全平台调研（iOS / macOS / Android / Windows / Linux）

> 目标：一套客户端覆盖 iOS、macOS、Android、Windows、Linux 六端；前端 UI 与
> 服务端 WebUI（`/ui`，微信风格单页）同款即可，不追求原生观感。
> 使用场景：家庭自用、服务端在 opi5p 私网（nginx + HTTPS vhost），无上架商店需求。

## 结论先行

**推荐：把现有 WebUI PWA 化作为唯一客户端基座**（一端开发、六端可用、零签名/
零商店成本）；之后若想要 Android 端更"像 App"的安装体验，再叠一层
Capacitor 壳出 APK。现有 Tauri + Element Plus 桌面客户端功能已被 WebUI 全覆盖，
建议迁移完成后退役，避免双前端维护。

## 现状盘点

| 已有 | 状态 |
| --- | --- |
| 服务端 WebUI `/ui` | 微信风格四页（库存/墨水屏/固件 OTA/设置），移动优先布局，touch 手势（左滑/甩动）、底部弹层、FLIP 动画齐全；`Cache-Control: no-cache`，刷新即最新 |
| 鉴权 | 页内顶栏输 token 存 localStorage，API 走 Bearer 头，天然适配任意 WebView |
| 网络入口 | nginx vhost `food.<host>.zhyi.xin`，zerossl 证书，HTTPS 就绪（PWA 硬性前提已满足） |
| 桌面客户端 `client/` | Tauri 2 + Vue 3 + Element Plus，仅桌面五页；与 WebUI 功能重复，Element Plus 偏桌面向，不适配手机 |
| WebUI 缺的 PWA 要素 | 无 `manifest.json`、无图标、无 `apple-touch-icon`、`viewport` 未带 `viewport-fit=cover`、`<title>` 还是"食管家 · 原型"、无 theme-color |

## 候选方案

### 方案 A：WebUI PWA 化（推荐基座）

做法：给 WebUI 补 PWA 要素（见文末清单），各平台浏览器"安装/添加到主屏幕"。

| 平台 | 获得形态 |
| --- | --- |
| iOS / iPadOS | 主屏幕 Web App（standalone 全屏、独立存储、图标）；iOS/iPadOS 26 起**任何网站**添加到主屏幕默认就以 Web App 打开，对本方案是顺风 |
| Android | Chrome "安装应用"，生成 launcher 图标 + standalone 窗口 |
| Windows | Edge/Chrome "安装应用"（独立窗口、开始菜单/任务栏图标） |
| macOS | Chrome "安装应用"（独立窗口、Dock 图标） |
| Linux | Chrome/Chromium "安装应用"（`.desktop` 桌面项） |

- 优点：一次改动六端生效；更新零成本（页面本就在服务端，no-cache 已配）；
  完全绕开苹果签名/商店审核；与"UI 类似 WebUI 就行"的诉求完全重合。
- 缺点：本质仍是浏览器引擎窗口（无自绘标题栏等原生细节）；桌面端体验是
  "无边框 Chrome 实例"；离线无壳（本项目离开家庭网络本就无法工作，影响小）。
- 工作量：**0.5–1 天**（基本是静态资源 + meta 追加，JS 零改动或极少）。

### 方案 B：Capacitor 壳（iOS/Android 真机安装包）

做法：Capacitor（当前 8.x，8.5.1，2026-08）新建壳工程，`server.url` 直接指向
`https://food.…/ui/`，WebView 加载远端页面，不复制前端代码。

| 平台 | 获得形态 |
| --- | --- |
| Android | 直接出 APK 侧载（家庭群发链接装即可，无需 Play 商店） |
| iOS | 出 ipa，但**绕不开签名**：免费 Apple ID 个人证书 7 天过期需反复重签；稳定自用要么 $99/年 开发者账号（TestFlight/自签），要么靠 PWA |
| 桌面 | 官方只有 iOS/Android/PWA，桌面需另配 Electron（社区维护）——不如方案 A/现状 |

- 优点：Android 端真 APK、图标/启动屏/全屏可控；后续若要本地插件（蓝牙、
  通知）有生态。
- 缺点：iOS 的签名/续签是持续成本；为"包一层网页"引入 Xcode/Android Studio
  双工具链；应用商店若上架会卡"纯网站套壳"审核（自用侧载无此问题）。
- 工作量：Android **1 天**；iOS **1 天 + 签名折腾**（且构建必须在 macOS 上做）。

### 方案 C：Tauri 2 全家桶（现有 client 延伸）

Tauri 2 自 2024-10 起稳定支持 iOS（9+）/Android（7+），2.9.x 已带
`tauri ios run` / `tauri android run`。两条子路线：

- **C1：现有 Vue + Element Plus 前端直接上移动端**——要为手机重做整套交互
  （Element Plus 桌面向），等于重写 UI，与"类 WebUI 即可"相悖。不推荐。
- **C2：Tauri 壳加载远端 WebUI**（webview `url` 指向 `/ui/`）——与方案 B 同质，
  但 Tauri 做壳要引入 Rust + Xcode + Android SDK 三套工具链，比 Capacitor 更重；
  远端内容 + IPC 的权限配置繁琐（remote domain 访问需显式声明）。

- 适用场景：只有当未来想在客户端里做**本地 BLE**（绕开服务端直连墨水屏）等
  原生能力时才值得回来选 Tauri/Capacitor；当前架构明确"客户端不碰蓝牙"。
- 工作量：C2 约 2–3 天（三端工具链 + 调试），iOS 同样需要 macOS + 签名。

### 方案 D：维持双前端（桌面 Tauri 现状 + 移动 PWA）

- 优点：不动现有代码。
- 缺点：每次加功能要写两遍（本次 OTA 功能就已出现 WebUI 有、桌面客户端逐步
  跟进的成本）；两套 UI 风格割裂。仅作为过渡态。

## 平台约束与注意事项

| 事项 | 说明 |
| --- | --- |
| iOS 构建门槛 | 任何"真 App"方案（B/C）都要求 macOS + Xcode；签名要么接受 7 天重签（免费账号，≤10 App ID），要么 $99/年。**PWA 是唯一零签名路径** |
| Android | APK 侧载零门槛；无需商店 |
| 桌面三端 | PWA 安装即用；现 Tauri 客户端打包链（app/deb/appimage/rpm）已通，保留与否只影响过渡期 |
| 网络可达性 | 服务端仅私网可达。家里 Wi-Fi 直接用；出远门需 Tailscale/WireGuard——Tailscale 可给 ts.net 域名签 HTTPS 证书，PWA 可照常安装使用 |
| WebView 兼容性 | iOS 用 WKWebView（Safari 引擎）、Android 用 Chromium WebView；WebUI 只用标准 touch/DOM/fetch/localStorage，无兼容性风险。甩动等手势两端均可 |
| token 持久化 | 安装到主屏幕的 Web App 拥有独立持久存储，localStorage token 不会被 Safari 7 天 ITP 清理误删（浏览器标签页内用才有此风险） |
| 数据实时性 | 不建议做离线缓存/Service Worker 缓存数据：离开服务端既看不到库存也推不了墨水屏；SW 最多缓存壳资源，价值低，可不做 |

## 推荐路线（分阶段）

1. **第一步（PWA 化，覆盖六端）**
   - `static/` 增加 `manifest.json`（name/short_name=食管家、`display: standalone`、
     `start_url: /ui/`、theme_color `#07C160` 一类微信绿、192/512 + maskable 图标）；
   - `index.html`：`<title>` 去掉"原型"、补 `apple-touch-icon`（180×180）、
     `theme-color`、viewport 加 `viewport-fit=cover`（safe-area CSS 已在用）；
   - 图标生成一张源图导出多尺寸即可。
2. **第二步（按需）**：给家里 Android 设备出 Capacitor APK 壳（`server.url` 指
   vhost），获得更像"App"的安装体验；iOS 设备继续用主屏幕 Web App。
3. **第三步（收尾）**：WebUI 功能确认无缺口后，`client/` Tauri 桌面客户端退役
   （或仅保留 flake 里的 client devShell 供历史参考），README/架构文档同步。

## PWA 化落地清单（供实现时勾选）

- [ ] `static/manifest.json` + `index.html` `<link rel="manifest">`
- [ ] 图标：192、512、maskable-512、`apple-touch-icon` 180（PNG）
- [ ] `<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no, viewport-fit=cover">`
- [ ] `<meta name="theme-color">` 与 iOS `apple-mobile-web-app-*` meta
- [ ] `<title>` 定名（如"食管家"）
- [ ] nginx/静态层确认 manifest 与图标带正确 Content-Type（FastAPI StaticFiles 已覆盖）
- [ ] iOS 真机验收：standalone 打开、safe-area 不遮挡底栏、token 输入一次后续免输
- [ ] Android/Edge/Chrome 桌面验收：安装入口出现、图标正确、窗口独立

## 参考

- [Tauri 2.0 Stable（含 iOS/Android 支持）](https://v2.tauri.app/blog/tauri-20/) ·
  [Tauri Develop（移动端开发要求）](https://v2.tauri.app/develop/) ·
  [Tauri 远端内容讨论 #986](https://github.com/tauri-apps/tauri/issues/986)
- [Capacitor 官网（iOS/Android/PWA）](https://capacitorjs.com/) ·
  [Capacitor Releases（8.5.1，2026-08）](https://github.com/ionic-team/capacitor/releases) ·
  [Capacitor 8 发布公告（SPM 默认化）](https://ionic.io/blog/announcing-capacitor-8) ·
  [远端 URL 配置 server.url 讨论](https://stackoverflow.com/questions/55894716/)
- [iOS/iPadOS 26 主屏幕 Web App 行为变化（默认以 Web App 打开）](https://mjtsai.com/blog/2025/10/03/web-apps-in-ios-26/) ·
  [MacRumors 添加 Web App 教程](https://www.macrumors.com/how-to/save-safari-bookmark-web-app-iphone-home-screen/)
