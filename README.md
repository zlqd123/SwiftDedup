# SwiftDedup

多源聚合监控 + 极速去重的可插拔框架：各内容平台由 `platform/` 目录下的
**平台插件**接入，框架自身不含任何具体平台知识。

## 架构

```
SwiftDedup/
├── main.py                # 编排入口：插件发现 → 共享浏览器 → 平台服务 → 控制台/API
├── monitor/
│   ├── core/platform.py   # 平台插件注册表 + 目录自动发现
│   ├── dedup/             # 判重引擎：指纹 / 多池竞速 / 信源注册表 / 播种器
│   ├── console.py         # 网页控制台：门户 / 登录 / 配置 / 操作账号
│   ├── sd_api.py          # 聚合管理：信源管理面板 / 事件页 / 订阅流 / 账本查询
│   ├── hub_api.py         # 平台路由统一挂载（add_service/add_registrar）
│   └── hub_browser.py     # 全插件共享的 Playwright 底层
├── platform/              # 平台插件安装位（git clone 各平台插件仓库到此）
├── config.yaml            # 用户配置（不入库）
└── data/                  # 运行数据（不入库）
```

## 平台插件接入

1. 把平台插件仓库 clone 为 `SwiftDedup/platform/`（或其一级子目录）；
2. 启动 `python main.py`，框架自动检测 `platform/` 并注册其中的平台；
3. 按 `platform/` 内各平台 README 填写 `config.yaml` 的 `platforms.<name>` 小节。

插件需提供（见 `monitor/core/platform.py` 的注释契约）：

- `name` / `display_name` / `domains` / `manual_url` / `create_service(...)`
- 判重钩子：`publish_ts` / `extract_pid` / `fingerprint_text` / `lottery_filtered`
- 展示钩子：`display_text` / `forward_note` / `image_urls` / `post_link`
- 播种钩子：`iter_seed_cache`

**想接入一个新平台？** 全部契约、生命周期、钩子语义、数据布局与操作
清单见 [`docs/platform-dev.md`](docs/platform-dev.md)（平台插件开发指南）。

## 端点总表

默认端口见 `config.yaml` 的 `port`（示例 `28801`）。`/data/*` 都是只读查询，
`/api/*` 是管理动作，`/ui/*` 是网页面板。

### 网页面板

| 端点 | 说明 |
|---|---|
| `/` | 入口页（重定向到门户） |
| `/ui/` | 门户（面板导航 + 运行概览） |
| `/ui/login/` | 登录页（扫码） |
| `/ui/config/` | 配置编辑（多数项需重启进程生效；另有少数项可热改） |
| `/ui/opener/` | 操作账号页（打开账号页 / 二维码） |
| `/ui/opener/{平台}` | 让共享浏览器打开指定平台的账号主页 |
| `/ui/sd/` | 信源管理面板（信源编排 / 判重日志 / 别名账本 / 抓取名单） |
| `/ui/events/` | 信源事件面板（重要事件流 / 判重明细） |
| `/ui/{平台}/` | 平台自有监控面板（由平台插件注册，见插件仓文档） |

### 基础与健康

| 端点 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 健康检查（含判重引擎状态摘要） |

### 登录

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/login/status` | GET | 各平台登录状态汇总 |
| `/api/login/{平台}` | POST | 触发指定平台登录（出二维码）；body 可带 `manual` 等 |
| `/data/login/qrcode/{平台}` | GET | 登录二维码实时落盘图（面板点开时拉取） |
| `/data/sd/qrcode/{平台}` | GET | 同上（聚合管理面板使用的别名路径） |

### 配置

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/config` | GET | 读取当前 `config.yaml` 结构化视图 |
| `/api/config` | POST | 保存配置（写回 `config.yaml`） |

### 聚合查询（data，只读）

| 端点 | 方法 | 说明 |
|---|---|---|
| `/data/sd/sources` | GET | 信源列表（含各池计数、账号/待认领） |
| `/data/sd/source/{sid}` | GET | 单个信源详情 |
| `/data/sd/source/{sid}/posts?limit=&before=` | GET | 信源帖子流（canonical + 已放行别名，按发布时间倒序，可翻页） |
| `/data/sd/post/{aid}` | GET | 聚合号反查（一帖多聚合号均可命中），含全部重复侧别名 |
| `/data/sd/alias/{平台}/{帖子id}` | GET | 帖子别名反查（canonical / alias / 聚合号） |
| `/data/sd/alias/user/{平台}/{uid}` | GET | 某账号的聚合汇总 |
| `/rss/sd/{sid}?limit=` | GET | 信源订阅流 RSS（正文尾注"来自{平台}"） |
| `/atom/sd/{sid}?limit=` | GET | 信源订阅流 Atom |

订阅流构成 = 该池 canonical 领号帖 + 手动放行(released)的重复帖；
信源关掉判重（`dedup: false`）即不产账本、流为空。

### 聚合管理（api）

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/sd/status` | GET | 引擎状态 / 播种状态 / 未认领计数（读前自动做映射文件热重载检查） |
| `/api/sd/seed` | POST | 触发历史缓存播种；body `{force: true}` 可强制重播 |
| `/api/sd/log/dedup?limit=` | GET | 判重日志（倒序，含 80 字内容快照） |
| `/api/sd/release` | POST | 放行重复帖进订阅流；body `{platform, post_id}` |
| `/api/sd/source` | POST | 新建信源；body `{id?, name}` |
| `/api/sd/source/{sid}` | PATCH | 信源编辑：`{name}` 改名、`{accounts: {平台:[uid]}}` 关联账号（换源自动重发聚合号 D19）、`{dedup: bool}` 分组判重开关 |
| `/api/sd/source/{sid}` | DELETE | 删除信源（其下账号回到待认领，号段不回收） |
| `/api/sd/source/{sid}/account/{平台}/{uid}` | DELETE | 解绑单个账号 |
| `/api/sd/watchlist` | GET | 抓取名单（平台 watchlist，昵称对照） |
| `/api/sd/watchlist` | POST | 名单加账号 |
| `/api/sd/watchlist/{平台}/{uid}` | DELETE | 名单移除账号 |
| `/api/sd/link` | POST | 名单账号 ↔ 信源绑定/解绑；body `{platform, uid, sid}`（sid≤0 解绑） |
| `/api/sd/nicknames` | GET | 昵称全集（含未订阅账号缓存）+ 跨平台同名对照 |
| `/api/sd/nickname` | POST | 手工登记昵称 |

### 实时推送（SSE）

| 端点 | 说明 |
|---|---|
| `/ui/sd/stats` | 信源面板轮询统计 |
| `/ui/sd/events` | SSE：判重日志增量推送 |

### 平台插件侧端点

平台插件在自己注册时挂载 `/ui/{平台}/`（面板）、`/data/{平台}/...`（只读数据）、
`/api/{平台}/...`（订阅 / 临时抓取 / 调试）、`/rss/{平台}/{uid}` 与 `/atom/{平台}/{uid}`
（单账号订阅）。完整清单见插件仓 README（`platform/README.md`）。

登录失败不会退出程序：控制台始终可用，未登录平台稍后可重试。

## 判重

指纹只比文字（含标题与引用原帖）：逐字哈希级 + 4字符 n-gram 包含率模糊级，
其中指纹账本写 `/data/dedup/`。注册表内每个信源可独立控制判重：`dedup: false`
关闭该池判重（不比对、不领聚合号），或在管理面板信源卡用「去重」开关热切。
（历史 shadow/enforce 双模式已移除，信源级开关是唯一控制。）

回归基准：判重语料回归随平台插件仓库提供（`platform/tools/verify_corpus.py`，
基准 canonical 61 / duplicate 22，见插件仓 README）。

## License

AGPL-3.0（见 LICENSE）
