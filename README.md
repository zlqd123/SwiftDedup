# SwiftDedup

多源聚合监控 + 极速去重的可插拔框架：各内容平台由 `platform/` 目录下的
**平台插件**接入，框架自身不含任何具体平台知识。

## 架构

```
SwiftDedup/
├── main.py                # 编排入口：插件发现 → 共享浏览器 → 平台服务 → 控制台/API
├── monitor/
│   ├── core/platform.py   # 平台插件注册表 + 目录自动发现
│   ├── dedup/             # 判重引擎：指纹/泯池竞速/信源注册表/播种器
│   ├── console.py         # 网页控制台：门户 / 登录 / 配置
│   ├── sd_api.py          # 聚合管理：信源管理(/ui/sd/)与信源面板(/ui/events/)
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

## 网页控制台

- `/ui/` 门户；`/ui/login/` 登录扫码；`/ui/opener/` 操作账号；`/ui/config/` 配置编辑
- `/ui/sd/` 信源管理（信源编排、判重日志、别名账本）；`/ui/events/` 信源面板
- `/ui/<平台>/` 各平台自有监控面板（由平台插件注册）

登录失败不会退出程序：控制台始终可用，未登录平台稍后可重试。

## 判重

指纹只比文字（含标题与引用原帖）：逐字哈希级 + 4字符 n-gram 包含率模糊级，
其中指纹账本写 `/data/dedup/`。注册表内每个信源可独立控制判重：`dedup: false`
关闭该池判重（不比对、不领聚合号），或在管理面板信源卡用「去重」开关热切。

回归基准：判重语料回归随平台插件仓库提供（`platform/tools/verify_corpus.py`，
基准 canonical 61 / duplicate 22，见插件仓 README）。

## License

AGPL-3.0（见 LICENSE）
