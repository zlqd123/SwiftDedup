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
"""

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger

from monitor.hub_config import HubConfig, PlatformConfig
from monitor.hub_browser import HubBrowser
from monitor.hub_api import HubAPIServer
from monitor.maintenance import MaintenanceScheduler

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

        # 0. 平台插件目录自动检测：存在 platform/ 即注册其中的平台
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

            # 7. 维护调度循环（ tick 每 20 秒一次，含投票顺延）
            logger.info("系统运行中... 按 Ctrl+C 停止")
            while self.is_running:
                try:
                    if self.scheduler:
                        await self.scheduler.tick()
                except Exception as e:
                    logger.error(f"维护调度异常: {e}")
                await asyncio.sleep(20)
        except Exception as e:
            logger.exception(f"运行异常: {e}")
        finally:
            await self.stop()

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
        """停止监控系统（每一段限时收尾，学习自两个前身）"""
        logger.info("正在停止... (再按一次 Ctrl+C 可强制退出)")
        if self._seed_task:
            self._seed_task.cancel()
        for name, svc in list(self.services.items()):
            try:
                await asyncio.wait_for(svc.stop(), timeout=10)
            except Exception as e:
                logger.warning(f"平台 {name} 收尾超时/异常: {e}")
        if self.api:
            try:
                await asyncio.wait_for(self.api.stop(), timeout=10)
            except Exception as e:
                logger.warning(f"API 收尾超时/异常: {e}")
        if self.hub_browser:
            try:
                await asyncio.wait_for(self.hub_browser.stop(), timeout=15)
            except Exception as e:
                logger.warning(f"浏览器收尾超时/异常: {e}")
        logger.info("SwiftDedup 已停止")


async def main():
    await SwiftDedupApp("config.yaml").start()


if __name__ == "__main__":
    asyncio.run(main())
