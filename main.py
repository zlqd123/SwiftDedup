#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""SwiftDedup — 多平台动态聚合监控（入口）

架构：
    HubBrowser      一个 Playwright + 一个持久化 context（全平台共用登录 profile）
    各平台 Service  每平台一个监控页（由 platform/ 插件提供），转换成标准 Post
    HubAPIServer    一个 HTTP 端口承载全部平台路由（/{platform}/api/...、/{platform}/rss/{id}）
    Maintenance     全网统一每日维护重启（窗口摇号 + 全平台投票）
    sources.yaml    信源映射（Phase 2：跨平台竞速去重，先落位不生效）

每个平台可在 config.yaml 的 platforms.<name>.enabled 单独开关；平台代码
不认识其它平台，互不影响。

崩溃可观测：
    hub.log 只记录 loguru 业务日志；进程级死亡（未捕获异常 / C 层 abort /
    被系统强杀）不会经过 loguru。因此入口处注册：
    - sys.excepthook        未捕获异常 → data/logs/crash.log（含完整 traceback）
    - threading.excepthook  子线程未捕获异常 → 同上（aiohttp/playwright 常驻线程）
    - faulthandler          段错误/abort 等硬崩溃 → 同文件（原生栈 dump）
    控制台输出完全不受影响，bat 保持原样（彩色实时日志）。
"""

import asyncio
import faulthandler
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger

from monitor.hub_config import HubConfig, PlatformConfig
from monitor.hub_browser import HubBrowser
from monitor.hub_api import HubAPIServer
from monitor.maintenance import MaintenanceScheduler

# 启动前端口预检：旧实例关窗口后要 8~10s 才真正退出（实测），等它比崩掉重开划算
PORT_WAIT_TRIES = 4       # 最多 4 轮
PORT_WAIT_INTERVAL = 5    # 每轮间隔 5s（合计 15s，覆盖实测的 8~10s 窗口）
# 收尾总闸：端口在第一秒就释放了，剩下的清理再慢也不影响新实例启动
STOP_TIMEOUT = 12

# 进程级崩溃日志：hub.log 管不到的地方（未捕获异常/硬崩溃）统一落到这里。
# 控制台照常输出，不受影响；文件只追加，按大小轮转避免无限膨胀。
CRASH_LOG = Path(__file__).resolve().parent / "data" / "logs" / "crash.log"
CRASH_LOG_MAX_BYTES = 2 * 1024 * 1024   # 超过 2MB 轮转为 crash.log.1


def _install_crash_hooks() -> None:
    """把 hub.log 覆盖不到的进程级死亡全部留档到 crash.log。

    覆盖三类死亡：
    1. 主线程未捕获异常（traceback 默认打到 stderr，窗口一关就没了）
    2. 子线程未捕获异常（threading 默认只打到 stderr，同样会丢）
    3. 硬崩溃（faulthandler：段错误/abort/被强杀时 dump 所有线程栈）
    """
    try:
        CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        if CRASH_LOG.exists() and CRASH_LOG.stat().st_size > CRASH_LOG_MAX_BYTES:
            crash_prev = CRASH_LOG.with_suffix(".log.1")
            if crash_prev.exists():
                crash_prev.unlink()
            CRASH_LOG.rename(crash_prev)
    except Exception:
        pass  # 轮转失败不拦截启动，crash.log 大点也能用

    def _append(text: str) -> None:
        try:
            with open(CRASH_LOG, "a", encoding="utf-8", errors="replace") as f:
                f.write(text)
        except Exception:
            pass  # 崩溃路径上写日志失败只能放弃，绝不再抛

    def _fmt(header: str, exc: BaseException) -> str:
        body = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        return (f"\n{'=' * 60}\n"
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {header}\n"
                f"pid={os.getpid()} argv={' '.join(sys.argv)}\n"
                f"{'=' * 60}\n{body}\n")

    # 1. 主线程未捕获异常
    prev_hook = sys.excepthook

    def _sys_hook(t, v, tb):
        _append(_fmt("UNCAUGHT EXCEPTION (main thread)", v))
        try:
            prev_hook(t, v, tb)   # 保留默认行为：stderr 照打
        except Exception:
            pass

    sys.excepthook = _sys_hook

    # 2. 子线程未捕获异常（Python 3.8+）
    if hasattr(threading, "excepthook"):
        prev_thread_hook = threading.excepthook

        def _thread_hook(args):
            exc = args.exc_value
            name = args.thread.name if args.thread else "unknown"
            if isinstance(exc, SystemExit):
                pass  # 线程里主动 SystemExit 是正常退出手段，不算崩溃
            elif isinstance(exc, BaseException):
                _append(_fmt(f"UNCAUGHT EXCEPTION (thread '{name}')", exc))
            else:
                _append(f"\n[crash] thread '{name}' exception, no exc_value: {args}\n")
            try:
                prev_thread_hook(args)
            except Exception:
                pass

        threading.excepthook = _thread_hook

    # 3. 硬崩溃（段错误/abort）：无法格式化，只能 dump 原生栈
    try:
        fh = open(CRASH_LOG, "a", encoding="utf-8", errors="replace")
        faulthandler.enable(fh)
        # 不 close：faulthandler 需要句柄在进程生命周期内保持可用
    except Exception:
        pass  # faulthandler 不可用就退化为只抓 Python 层异常



class SwiftDedupApp:
    """监控系统主类 — 编排 HubBrowser / 平台服务 / 统一 API / 维护调度"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config = HubConfig(config_path)
        self.hub_browser: Optional[HubBrowser] = None
        self.api: Optional[HubAPIServer] = None
        self.scheduler: Optional[MaintenanceScheduler] = None
        self.services: Dict[str, object] = {}
        # Phase 2 — 信源判重
        self.registry = None
        self.ledger = None
        self.engine = None
        self.sdhub = None
        self.console = None
        self.ready: Dict[str, object] = {}
        self._seed_task = None
        self.is_running = False
        self._stopping = False
        api_cfg = (self.config.api or {})
        self.api_host = api_cfg.get('host', '127.0.0.1')
        self.api_port = int(api_cfg.get('port', 28801))
        self._console_handler = None      # 必须持有引用，否则 ctypes 回调会被 GC

    # ========== 端口预检（方案 A）==========
    # 旧实例「关窗口」后进程要 8~10s 才消失，这段时间 bind 必崩（WinError 10048）。
    # 与其让浏览器白启动一遍再崩，不如在一切开始之前就盯着端口。

    @staticmethod
    def _can_bind(host: str, port: int) -> bool:
        """能 bind 就说明端口空闲。

        Windows 的坑：已有 0.0.0.0:port 在监听时，再 bind 127.0.0.1:port
        仍然会成功（实测），所以两个地址都要试——0.0.0.0 覆盖通配监听，
        具体地址覆盖「别人只绑了 127.0.0.1」的情况。
        """
        for addr in dict.fromkeys([host, '0.0.0.0']):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):     # Windows：独占，避免假空闲
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                s.bind((addr, port))
            except OSError:
                return False
            finally:
                s.close()
        return True

    @staticmethod
    def _port_owner(port: int) -> str:
        """端口占用者描述，形如 `PID 21516 (python.exe)`；拿不到就返回空串"""
        try:
            out = subprocess.run(['netstat', '-ano', '-p', 'TCP'],
                                 capture_output=True, timeout=6)
            text = out.stdout.decode('gbk', 'ignore')
            pids = []
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3].upper() == 'LISTENING' \
                        and parts[1].endswith(f':{port}'):
                    if parts[4] not in pids:
                        pids.append(parts[4])
            if not pids:
                return ''
            desc = []
            for pid in pids:
                name = ''
                try:
                    q = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH', '/FO', 'CSV'],
                                       capture_output=True, timeout=6)
                    first = q.stdout.decode('gbk', 'ignore').strip().splitlines()[0]
                    if first.startswith('"'):
                        name = first.split('","')[0].strip('"')
                except Exception:
                    pass
                desc.append(f"PID {pid}{f' ({name})' if name else ''}")
            return '、'.join(desc)
        except Exception:
            return ''

    async def _wait_port_free(self):
        """启动前检查端口；被占用就每 5s 重试一次，最终仍被占则明确报错退出。

        所有状态同时打到 stdout（print）：端口避让发生在 loguru 初始化之前
        的场景（历史问题：曾把这条链路全部吞进隐藏窗口，用户只看到「全黑」），
        print 能保证即使 loguru 尚未就绪，窗口里也有可见的过程输出。
        """
        def _echo(msg: str):
            ts = time.strftime('%H:%M:%S')
            print(f"[{ts}] [端口] {msg}", flush=True)

        for i in range(1, PORT_WAIT_TRIES + 1):
            if self._can_bind(self.api_host, self.api_port):
                if i > 1:
                    msg = f"端口 {self.api_port} 已释放，继续启动"
                    _echo(msg)
                    logger.info(msg)
                else:
                    _echo(f"端口 {self.api_port} 空闲")
                return
            owner = self._port_owner(self.api_port)
            tip = f"（占用者：{owner}）" if owner else ""
            if i < PORT_WAIT_TRIES:
                msg = (f"端口 {self.api_port} 被占用{tip}，"
                       f"{PORT_WAIT_INTERVAL}s 后重试 [{i}/{PORT_WAIT_TRIES}]")
                _echo(msg)
                logger.warning(msg)
                await asyncio.sleep(PORT_WAIT_INTERVAL)
            else:
                msg = (f"端口 {self.api_port} 仍被占用{tip}，启动中止。"
                       f"可执行 netstat -ano | findstr :{self.api_port} 查看后 "
                       f"taskkill /F /PID <PID>")
                _echo(msg)
                logger.error(msg)
        raise SystemExit(1)

    # ========== 关窗口快速退出 ==========
    # Ctrl+C 走 Python 的 SIGINT（优雅收尾）；「关窗口」是 CTRL_CLOSE_EVENT，
    # Python 的 handler 不管它，Windows 只给 5s 然后强杀，期间进程半死不活还占着端口。
    # 这里抢先接管：立刻落账 + TerminateProcess，进程秒退、端口秒放。

    def _install_console_close_handler(self):
        if os.name != 'nt':
            return
        try:
            import ctypes
            from ctypes import wintypes

            def _on_event(event: int) -> bool:
                # 2=CTRL_CLOSE_EVENT  5=CTRL_LOGOFF  6=CTRL_SHUTDOWN
                if event not in (2, 5, 6):
                    return False          # Ctrl+C / Ctrl+Break 交回 Python 正常处理
                self._quick_exit(event)
                return False

            self._console_handler = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(_on_event)
            if not ctypes.windll.kernel32.SetConsoleCtrlHandler(self._console_handler, True):
                logger.debug("SetConsoleCtrlHandler 返回 0（关窗口仍走系统默认流程）")
            else:
                logger.debug("已注册控制台关闭快速退出")
        except Exception as e:
            logger.debug(f"注册控制台关闭处理失败（不影响运行）: {e}")

    def _quick_exit(self, event: int = 0):
        """关窗口专用：落账 → 立即终止进程（不做 asyncio 收尾，Windows 只给 5s）"""
        try:
            logger.warning(f"收到控制台关闭事件({event})，立即退出")
        except Exception:
            pass
        try:
            if self.ledger:
                self.ledger.flush(force=True)
        except Exception:
            pass
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.TerminateProcess(kernel32.GetCurrentProcess(), 0)
        finally:
            os._exit(0)          # TerminateProcess 万一失败，也在这一句走掉

    # ========== 日志 ==========
    # 平台代码统一走 loguru；Phase 1 共用一个 hub 日志文件
    # （后续可用 logger.bind(platform=...) 按平台拆分日志）

    def _setup_logging(self):
        # Windows 控制台默认 GBK，emoji/生僻字会让 print 与 loguru 抛
        # UnicodeEncodeError（曾直接把二维码登录流程打挂）—— 统一转 UTF-8 且
        # 无法编码的字符以 ? 代替，不让编码问题打断业务。
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass
        log_cfg = self.config.logging

        # [来源] 前缀：按 loguru record 的调用模块归属框架或平台
        # （插件加载后的模块名形如 sdplat.<平台名>.service，见 core/platform.py）
        def _source(record) -> str:
            parts = (record.get('name') or '').split('.')
            if parts and parts[0] in ('sdplat', 'platform') and len(parts) > 1:
                key = parts[1]
                from monitor.core.platform import display_name
                return display_name(key) or key or '框架'
            return '框架'

        def _fmt_console(record):
            return ("<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                    "<level>{level: <8}</level> | "
                    f"<cyan>[{_source(record)}]</cyan> "
                    "<level>{message}</level>\n{exception}")

        def _fmt_file(record):
            return ("{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
                    f"[{_source(record)}] {{message}}\n{{exception}}")

        logger.remove()
        logger.add(sys.stdout, level=log_cfg.get('console_level', 'INFO'), colorize=True,
                   format=_fmt_console)
        log_file = Path(log_cfg.get('file', './data/logs/hub.log'))
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.add(log_file, level=log_cfg.get('file_level', 'DEBUG'), encoding="utf-8",
                   format=_fmt_file,
                   rotation="00:00",
                   retention=f"{log_cfg.get('backup_days', 7)} days")
        logger.info("日志系统初始化完成")

    # ========== 信号 ==========
    # 规则与两个前身一致：第一次 Ctrl+C 走限时优雅收尾，第二次强制退出

    def _signal_handler(self, signum, frame):
        logger.info(f"收到信号 {signum}，准备退出...")
        if self._stopping:
            # 第二次 Ctrl+C：优雅退出大概率卡在 aiohttp SSE 收尾或 Windows
            # 驱动收尾，直接强制退出（信号处理里不能 await，只能 os._exit）
            logger.warning("收到第二次退出信号，强制退出")
            os._exit(130)
        self._stopping = True
        self.is_running = False

    # ========== 生命周期 ==========

    def _make_service(self, name: str, platform_config):
        from monitor.core.platform import get as _plat_get
        plugin = _plat_get(name)
        if plugin is None:
            raise ValueError(f"平台 {name} 无插件：请把平台代码仓库放入 platform/ 后重启")
        return plugin.create_service(platform_config, self.hub_browser)

    async def start(self):
        self._setup_logging()
        self.is_running = True

        # 0. 端口预检 + 关窗口快速退出（放在浏览器启动之前：冲突时 0 成本失败）
        self._install_console_close_handler()
        await self._wait_port_free()

        # 0.5 平台插件目录自动检测：存在 platform/ 即注册其中的平台
        from monitor.core.platform import load_plugins, names as plat_names
        load_plugins(Path(__file__).parent / 'platform')
        logger.info(f"[平台] 插件目录检索完成：注册 {len(plat_names())} 个")

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        enabled = [n for n, s in (self.config.platforms or {}).items()
                   if (s or {}).get('enabled', False)]
        logger.info("=" * 60)
        logger.info(f"SwiftDedup 启动 ({'有头' if self.config.development else '无头'}模式) "
                    f"启用平台: {', '.join(enabled) if enabled else '（无）'}")
        logger.info("=" * 60)

        try:
            # 1. 共享浏览器（一个 playwright + 一个登录 profile）；无平台启用时跳过
            if enabled:
                self.hub_browser = HubBrowser(self.config)
                await self.hub_browser.start()
            else:
                logger.warning("未启用任何平台插件：仅启动控制台与判重层"
                               "（把平台插件仓库放入 platform/ 后重启即可接入）")

            # 启动过程中按了 Ctrl+C：别再往后走了（否则要等登录/扫码才收尾）
            if not self.is_running:
                logger.info("收到退出信号，跳过后续启动步骤")
                return

            # 2. Phase 2 — 判重引擎（服务前建好，落盘回调即可用）
            dd = self.config.dedup
            if bool(dd.get('enabled', True)):
                dedup_dir = dd.get('data_dir', './data/dedup')
                from monitor.dedup.registry import SourceRegistry
                from monitor.dedup.ledger import Ledger
                from monitor.dedup.engine import DedupEngine
                from monitor.dedup.seeder import Seeder
                from monitor.sd_api import SDHub
                self.registry = SourceRegistry(dd.get('sources_file', 'sources.yaml'),
                                               dedup_dir)
                self.ledger = Ledger(dedup_dir)
                self.engine = DedupEngine(self.config, self.registry, self.ledger)
                self.sdhub = SDHub(self.config, self.engine, self.registry)
                self.sdhub.seeder = Seeder(self.engine, dd.get('seeds') or [])
                logger.info(f"[判重] 引擎就绪 (信源 {len(self.registry.all_sources())}, "
                            f"日志 data/dedup/)")

            # 3. 平台"准备"阶段（不登录）：建存储/浏览器/API 对象
            #    → 路由可注册 → 控制台立刻可用（用户可在网页扫码）
            for name in enabled:
                platform_config = PlatformConfig(name, self.config)
                try:
                    svc = self._make_service(name, platform_config)
                    if self.engine:
                        svc.engine = self.engine
                    svc.on_login_ok = lambda p=name: self._platform_ready(p)
                    await svc.prepare()
                    self.services[name] = svc
                except Exception as e:
                    logger.error(f"平台 {name} 准备异常: {e}")

            # 4. 统一 API + 控制台（一个端口承载所有平台的路由与面板）
            self.api = HubAPIServer(self.config)
            for svc in self.services.values():
                self.api.add_service(svc)
            if self.sdhub:
                self.sdhub.services = self.services
                self.api._sd_engine = self.engine
                self.api.add_registrar(self.sdhub.register)
            from monitor.console import Console
            self.console = Console(self.config, self.engine, self.registry)
            self.console.services = self.services
            self.api.add_registrar(self.console.register)
            await self.api.start()
            self.console.open_browser()      # 启动后自动打开控制台

            # 5. 平台"登录"阶段（此时控制台已上线，可网页扫码/重试）
            #    并行登录：两个平台的二维码同时出现，互不等待
            async def _start_one(name, svc):
                if not self.is_running:      # 已按 Ctrl+C，不再逐个登录
                    return False
                try:
                    ok = await svc.start()
                except Exception as e:
                    logger.error(f"平台 {name} 启动异常: {e}")
                    ok = False
                if ok:
                    logger.info(f"✅ 平台 {name} 就绪")
                else:
                    logger.warning(f"⚠️ 平台 {name} 未就绪（未登录），"
                                   f"控制台 /ui/login/ 可扫码重试")
                return ok

            if self.services:
                await asyncio.gather(*[_start_one(n, s)
                                       for n, s in list(self.services.items())])
            self.ready = {n: s for n, s in self.services.items()
                          if (s.login_state or {}).get('state') == 'logged_in'}
            if not self.ready:
                logger.warning("⚠️ 暂无平台登录成功，进程保持运行以便网页扫码登录")

            # 5.5 历史播种（仅学习；幂等；后台跑以免阻塞首批监控）
            if self.sdhub and self.sdhub.seeder:
                self._seed_task = asyncio.create_task(self._run_seed())

            # 6. 维护调度（全浏览器共用一份摇号计划，全平台投票）
            backends = {name: svc.bm for name, svc in self.services.items()
                        if svc.bm is not None}
            self.scheduler = MaintenanceScheduler(self.config, backends, self.hub_browser)
            if self.scheduler._enabled:
                logger.info(f"🗓 每日维护重启已启用 "
                            f"(窗口 {self.scheduler._window_start}:00-{self.scheduler._window_end}:00)")

            # 7. 主循环：1 秒粒度（Ctrl+C 后 1s 内进入收尾，不再等 20s）；
            #    维持 20s 一次维护 tick，并周期落盘账本 / 冲未关联批量日志
            logger.info("系统运行中... 按 Ctrl+C 停止")
            last_maint = 0.0
            while self.is_running:
                try:
                    now = time.monotonic()
                    if now - last_maint >= 20:
                        last_maint = now
                        if self.scheduler:
                            await self.scheduler.tick()
                    if self.ledger:
                        self.ledger.flush()
                    if self.engine:
                        self.engine.flush_unclaimed_if_stale()
                except Exception as e:
                    logger.error(f"主循环维护任务异常: {e}")
                await asyncio.sleep(1)
        except Exception as e:
            logger.exception(f"运行异常: {e}")
        finally:
            # 总闸：端口在 stop() 的第一秒就释放了，剩下的清理即使卡住，
            # 也不该拖住「关掉再重开」——超时就硬退，新实例进去照常 bind。
            try:
                await asyncio.wait_for(self.stop(), timeout=STOP_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(f"收尾超过 {STOP_TIMEOUT}s，强制退出（端口已释放，可直接重启）")
                os._exit(0)
            except Exception as e:
                logger.warning(f"收尾异常（不影响重启）: {e}")

    def _platform_ready(self, name: str):
        """平台在控制台扫码登录成功后回调：登记到维护调度并刷新状态"""
        svc = self.services.get(name)
        if svc is None:
            return
        self.ready[name] = svc
        if self.scheduler and svc.bm is not None:
            self.scheduler.backends[name] = svc.bm
        logger.info(f"✅ 平台 {name} 已就绪（控制台扫码登录）")

    async def _run_seed(self):
        """后台播种（spec §3）：仅学习旧缓存，幂等"""
        try:
            await self.sdhub.seeder.run()
        except Exception as e:
            logger.exception(f"[播种] 异常(不影响主流程): {e}")

    async def stop(self):
        """停止监控系统：**先把端口交出去**，再慢慢收尾（每段限时）"""
        logger.info("正在停止... (再按一次 Ctrl+C 可强制退出)")

        # 0. 第一件事：关监听。后面无论多慢，新实例都能立刻 bind
        if self.api:
            try:
                await asyncio.wait_for(self.api.stop_listener(), timeout=3)
            except Exception as e:
                logger.debug(f"释放监听端口失败: {e}")

        # 1. 账本先落盘（后面就算被强杀也不丢账）
        if self.ledger:
            try:
                self.ledger.flush(force=True)   # 节流写盘的兜底：退出前强制落账
            except Exception as e:
                logger.warning(f"账本落盘失败: {e}")

        if self._seed_task:
            self._seed_task.cancel()

        # 2. 各平台并行收尾（原来串行，两个平台最坏要 26s）
        async def _stop_one(name, svc):
            try:
                await asyncio.wait_for(svc.stop(), timeout=8)
            except Exception as e:
                logger.warning(f"平台 {name} 收尾超时/异常: {e}")

        if self.services:
            await asyncio.gather(*[_stop_one(n, s) for n, s in list(self.services.items())])

        # 3. HTTP 收尾（监听已关，这里只清残留连接）
        if self.api:
            try:
                await asyncio.wait_for(self.api.stop(), timeout=6)
            except Exception as e:
                logger.warning(f"API 收尾超时/异常: {e}")
        if self.hub_browser:
            try:
                await asyncio.wait_for(self.hub_browser.stop(), timeout=15)
            except Exception as e:
                logger.warning(f"浏览器收尾超时/异常: {e}")
        logger.info("SwiftDedup 已停止")


async def main():
    # 所有相对路径（./data/...、sources.yaml、config.yaml）统一锚定到项目根：
    # 无论从哪个目录启动，都落到同一套数据目录
    os.chdir(Path(__file__).resolve().parent)
    # 崩溃钩子要在一切业务之前装好：晚了就抓不到启动阶段的死亡
    _install_crash_hooks()
    await SwiftDedupApp("config.yaml").start()


if __name__ == "__main__":
    asyncio.run(main())
