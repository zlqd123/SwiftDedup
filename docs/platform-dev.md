# 平台插件开发指南

面向想在 SwiftDedup 框架上接入新内容平台的开发者。框架本体**零平台知识**，
一切平台能力（登录、抓取、存储、面板、判重钩子）都以插件形态放入
`platform/` 目录；框架用注册表把它们接到引擎、播种器与管理页。

> 快速开始只要三步：建平台包 → 实现 `plugin.py` 注册入口 → 重启框架。
> 本文其余部分是**契约详解**——每个字段/钩子/生命周期阶段的精确语义。

---

## 1. 总览与安装形态

```
SwiftDedup/                      ← 框架仓库（不认识任何具体平台）
├── main.py                      ← 编排入口（见 §4 生命周期）
├── monitor/
│   ├── core/platform.py         ← 插件注册表 + 目录自动发现
│   ├── dedup/engine.py          ← 判重引擎（ingest → 指纹 → 按信源分池比对）
│   ├── dedup/seeder.py          ← 历史缓存播种
│   ├── sd_api.py                ← 聚合管理页（信源管理 /ui/sd/、面板 /ui/events/）
│   ├── hub_api.py               ← 统一 HTTP 端口
│   └── hub_browser.py           ← 全插件共享的 Playwright 底层
└── platform/                    ← 插件安装位
    └── <平台仓库> 可整仓 clone 到这里，或 clone 到一级子目录
```

**发现规则**（`monitor/core/platform.py::load_plugins`）：

- `platform/plugin.py` 存在 → 作为 `sdplat` 包加载；
- 否则一级子目录 `<dir>/plugin.py` 存在 → 作为 `sdplat_<dir>` 包加载；
- `plugin.py` 模块级调用 `register(plugin)`（或暴露模块级 `PLUGIN` 对象），
  相对 import 携带兄弟包，如 `from .weibo import hooks as _wh`；
- 加载异常不影响其它插件（每目录独立 try）。

要点：整仓 clone 到 `platform/` 时，**全部平台共用 `sdplat.*` 模块前缀**
（如 `sdplat.weibo.service`）；日志来源前缀（`[微博]`/`[B站]`）就是按
调用模块的第二段映射到注册表 `display_name` 的，见 §6。

把 `platform/` 整个移走即可进入无平台模式：门户/登录/配置、判重引擎、
账本照常运行，只是没有任何平台接入。

## 2. 注册表 API（框架侧可调用的全体接口）

```python
from monitor.core.platform import (
    register, get, has, names,          # 注册与枚举
    display_name, display_map,          # 平台名 → 显示名（浏览器标题/标签）
    attr,                               # attr(name, attribute, default)
)
```

- `register(plugin)`：`plugin.name` 必填，重复注册=覆盖；显示名写入全局表；
- `attr(name, 'hook_name')`：框架只在**需要时**取钩子并直接调用，
  平台没有实现某钩子时框架走默认行为（如 `attr(...,'lottery_filtered')` 为
  None 则不做抽奖过滤）。

## 3. Plugin（注册入口）契约

`plugin.py` 中定义平台类并 `register(...)`。duck protocol：

### 3.1 元数据（框架静态使用）

| 属性 | 类型 | 用途 |
|---|---|---|
| `name` | str | 注册键，== config.yaml `platforms.<name>` 节名 == 目录名 |
| `display_name` | str | 页面显示名（门户卡片/顶栏/信源标签/日志前缀） |
| `domains` | tuple[str] | 平台主页域名（登录跳转校验、外链识别用） |
| `manual_url` | str | `/ui/opener/<name>` 打开有头浏览器时落地的页面 |
| `tag_css` | str | 管理页平台标签配色（如 `.tag.<name>{background:#e8f4ff;color:#38f}`），可选 |

### 3.2 服务工厂

```python
def create_service(self, platform_config, hub_browser) -> Service
```

- `platform_config`：`PlatformConfig` 包装（`.name`、`.monitor`、`.storage`、
  `.watchlist`、`.temp_user_ids`、`devtools` 等按 config.yaml 小节切好的属性）；
- `hub_browser`：共享浏览器（`HubBrowser` 实例，playwright + 持久化
  登录 profile，登录二维码直接用它开页面，勿自建 playwright）。

### 3.3 判重/展示/播种钩子（模块级函数即可，`_bind` 到类后成静态方法）

| 钩子 | 签名 | 调用方 | 语义 |
|---|---|---|---|
| `publish_ts` | (post) → float | engine 判重时间窗 | 帖子发布 Unix 秒；拿不到返回 None |
| `extract_pid` | (post) → str | engine 别名键 | 帖子唯一 id（账本 `alias.json` 主键） |
| `fingerprint_text` | (post) → str | engine 指纹归一化 | 判重输入文本：标题+正文+转发引用原文，不含评论 |
| `display_text` | (post) → str | 管理页/日志快照 | 面向人的展示文本（未归一化） |
| `forward_note` | (post) → str | 管理页转发帖卡片 | 转发补引说明，无则空串 |
| `lottery_filtered` | (post) → bool | engine 播种口径 | True=转发抽奖结果帖，判重/播种忽略 |
| `image_urls` | (post, base) → list[str] | 管理页图片列 | 图片直链（`base` 用于补相对地址） |
| `post_link` | (uid, post) → (link, author) | 管理页帖子链接 | 帖子外链与作者页 |
| `iter_seed_cache` | (path) → iter(lat, uid, post) | seeder.run() | 遍历平台旧缓存目录为 adr 播种 |

调用顺序（判重 ingest 全链路）：
`service 抓取 → engine.ingest(platform, uid, raw_post)`
→ `extract_pid`（账本别键） → `publish_ts`（时间） →
`fingerprint_text` → `FP.normalize/exact_hash/ngram_bags` →
指纹在所属信源的池内比对 → duplicate/canonical。纯图/纯视频（无文字）不判重
只落别名。其它钩子（展示/图片/转发）只在管理页与日志快照时调用。

注意：框架拿到的 `post` 是**平台原始字典**，所有字段都通过钩子解释，
`platform/` 内代码可完全自由地定义自己的数据形状。

## 4. Service 契约与生命周期（与 main.py 对应）

```python
class XxxService:
    name = 'xxx'

    async def prepare(self)              # 准备阶段（不登录）
    async def start(self) -> bool        # 登录 + 启动抓取；失败不抛异常
    async def stop(self)                 # ≤10s 收尾
```

框架按此 7 步编排（`main.py::start`）：

1. `load_plugins(platform/)` → 注册全部插件；
2. 建 `HubBrowser`（有启用平台时）与判重引擎（registry/ledger/engine/seeder）；
3. **准备阶段**：`svc = plugin.create_service(PlatformConfig, hub_browser)`；
   `svc.engine = engine`（判重回调用）；
   `svc.on_login_ok = 回调`（登录成功后登记维护调度）；
   `await svc.prepare()` —— **此阶段不登录**；
4. `api.add_service(svc)`：service 需提供
   - `.name`（路由前缀），
   - `.api`（平台 APIServer，有 `register_routes(app)`，内部注册
     `/data/<名称>/…` 只读数据、`/api/<名称>/…` 写操作、
     `/rss/<名称>/{uid}`、`/atom/<名称>/{uid}`、面板 `/ui/<名称>/`），
   - `.dashboard`（可选，APIServer 构造时注入，
     由它在 register_routes 末尾调 `dashboard.register(app, prefix='/ui/<名称>')`）；
5. **登录阶段**：`ok = await svc.start()`（此时控制台已上线，未登录平台
   网页可扫码重试；登录失败**不得抛异常**，返回 False 即可）；
   `svc.login_state`（dict: state= idle/logging_in/failed/logged_in、
   message、at）为登录页实时状态；
6. 播种（`seeder.run()` 后台跑，幂等）；
7. 维护调度 `svc.bm`（浏览器管理器，有 .page 与逃生逻辑的）登记，全平台投票重启。

判重接线（平台侧，抓到新帖时）：

```python
await self.storage.save_dynamic(author_id, dynamic)          # 平台自缓存
if self.engine:
    await self.engine.ingest(self.name, author_id, dynamic)  # 引擎记账
```

日志：统一 `from loguru import logger`；框架自动按调用模块加 `[来源]`
前缀（控制台/文件），平台代码**无需**自己加平台名。web 面板不需要过滤
前缀——面板自挂的 loguru sink 读原始 `record['message']`，天然无前缀。

## 5. 平台数据文件布局（约定俗成，框架只读）

```
data/<名称>/
├── watchlist.json    # users: [{uid, name}], nicknames: {uid: 昵称}
│                     # 管理页「昵称管理（跨平台）」直接热载它
├── cache/user_<uid>/<pid>/<pid>.json   # 帖子纯平台数据（只增不改）
├── qrcode.png        # 登录二维码流程的实时落盘文件
└── ...
```

- **watchlist.json** 决定监控哪些账号；`seeds`/播种器的
  `iter_seed_cache(path)` 按此布局遍历旧缓存；
- `data/dedup/`（别名/指纹/判重日志）是框架账本，插件只可经 engine 写入，
  不要直接修改。

## 6. 日志前缀与网页面板行为

- 控制台/文件日志每行自动加 `[来源]`：`[框架]` 或注册表显示名
  （`[微博]`/`[B站]`），由 `main._setup_logging` 按 record 调用模块
  (`sdplat.<平台名>.*`) 映射；
- **网页侧什么也不用做**：各平台面板挂的 loguru sink 读原始
  `record['message']`，聚合管理页读的是判重账本的结构化事件——都没前缀；
- 面板着自己的 `.dashboard.py/_classify` 解析平台关键日志（新动态作者、
  启动跳过聚合等），格式约定与本平台 service 打的日志保持一致即可。

## 7. 平台面板推荐结构（参考实现：各平台 dashboard.py/.html）

面板推荐为自足的**单文件页面**（`<平台>/dashboard.html` + 读盘服务），
不依赖 CDN：

- `poll()` 轮询 `fetch('/ui/<名称>/stats')`（1s/3s/5s/10s/暂停）；
- SSE 接 `EventSource('/ui/<名称>/api/events/stream')` 实时日志；
- 账号按拼音排序（Intl zh 升序 + 数字开头排尾），昵称→个人主页蓝链；
- 「只显示账号的位置一律昵称优先」；重要事件/非关注账号/实时日志 5 章节框
  可直接照搬现有平台模板（两平台面板 HTML 高度同构）。

`dashboard.py` 要点：
- `attach_logger`：挂只进内存的 loguru sink（DEBUG 全量 ring buffer），
  `dashboard_level` 只是页面默认展示级别；
- `stats()` 返回 system（页面存活/运行时长/重启/内存）/accounts/events/skip 等；
- `note_request`（API 中间件回调）供“请求数/失败/1小时”列；
- `_daily_tick`：日界切累计器（today_new/skipped 等）。

## 8. config.yaml 平台小节

`platforms.<name>.enabled: true` 之后，其余键全部是**读给插件自己**的
（框架只读语言的 enabled 开关，其余原样透传给插件）：

```yaml
platforms:
  <name>:
    enabled: true
    monitor:  {check_interval: 3}          # 抓取节奏
    storage:  {cache_dir: ./data/<name>/cache,
               max_per_user: 20,
               external_base_url: ...}
    watchlist:{file: ./data/<name>/watchlist.json, ...}
    temp_user_ids: []                       # 占位账号
```

保持与既有一键配置相同（hub_cfg.api.dashboard 控制面板开关,
`maintenance.browser_restart` 为全网统一维护，插件不要自建重启逻辑，
只需把 `bm` 暴露给调度器投票）。

## 9. 新增平台的操作清单

1. 建包 `platform/<名称>/`（**目录名即注册键**；仓库根 plugin.py 模式
   亦可，把类型名当注册键）；
2. `hooks.py` 实现 §3.3 全部钩子（未实现=框架默认行为，判重质量相应变差）；
3. 平台类 + `create_service`；service 按 §4 实现生命周期与路由注册；
4. 面板（可先省略——没有面板平台也能完整登录/抓取/判重）；
5. `plugin.py`：`register(_bind(平台类, hooks)())`；或暴露模块级 `PLUGIN = 平台类实例` 也行；
6. 平台 README（config.yaml 小节样例）；
7. 回归：`python tools/verify_corpus.py`（判重回归,新平台不破坏既有
   canonical 61 / duplicate 22 基线）；启动自检看「[平台] 插件目录检索完成：
   注册 N 个」，随后 `/ui/<名称>/` 面板与自动登录应全部可用。

## 10. 合规红线

- **框架仓不允许出现任何具体平台词/域名/品牌色**——平台化的语料、
  seed 目录、页面文案、配色都在插件仓；
- 插件仓可以自由使用平台名（注意保持本仓私有，watchlist 与缓存含
  真实账号信息，勿入库）；
- 框架的 `docs/` 目前仅本文件一份（用户显式要求保留）；
- 插件不得反向修改 `monitor/` 内的全局状态（除注册表 `register()` 与
  §5 约定的数据文件）；平台数据文件布局按 §5 约定扩展。
