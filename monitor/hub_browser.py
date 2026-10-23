# -*- coding: utf-8 -*-

"""全插件共用的浏览器底部层 — 一个 Playwright 实例 + 一个持久化 context

架构角色：
- 持有 playwright / context / user_data_dir（全平台共用一个登录 profile，
  各平台 Cookie 按域隔离，同一 context 互不干扰）；
- 启动参数合入实测的"A 级削内存参数"（各项均已真机验证稳定）；
- 生命周期（start / stop / restart）由 hub 统一编排——每日维护重启改为
  一次重启带回全部平台页（重启窗口、摇号逻辑见 monitor/maintenance.py）。

各平台页面的媒体阻断不再挂 context.route（两平台同时挂会互相拦截对方的
页面请求），而是平台代码自己 page.route；hub 只负责关闭 launch 自带的
about:blank 占位页（独立 renderer 进程，实测 ~74-80MB）。
"""

import os
import asyncio
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger


class HubBrowser:
    """Playwright 实例与持久化 context 的唯一持有者"""

    def __init__(self, config):
        self.config = config            # HubConfig
        self.playwright = None
        self.context = None
        self.headless: bool = not config.development
        self.profile: str = "shell"

    # ========== 生命周期 ==========

    async def start(self):
        from playwright.async_api import async_playwright
        cfg = self.config.browser
        self.headless = not self.config.development
        user_data_dir = Path(cfg.get('user_data_dir', './data/browser_profile'))
        user_data_dir.mkdir(parents=True, exist_ok=True)

        # ---- 维护档位 ----
        #   shell(默认) = A 级削内存参数 + chromium-headless-shell 轻量内核（无头时）。
        #   base        = 仅 A 级削内存参数，完整 chromium 内核（风控对照/回退）。
        #   single      = 【已废弃】--single-process 必现 TargetClosedError 崩溃，降级 shell。
        profile = os.environ.get("HUB_PROFILE", "shell").strip().lower()
        if profile == "single":
            logger.warning("🧊 HUB_PROFILE=single 已验证不可行（TargetClosedError 必崩），自动降级为 shell")
            profile = "shell"
        elif profile != "base":
            profile = "shell"
        self.profile = profile

        try:
            self.playwright = await async_playwright().start()
        except Exception as e:
            logger.error(f"Playwright 启动失败: {e}")
            raise

        try:
            self.context = await self._launch_context(user_data_dir, profile)
        except Exception as e:
            if profile == "shell":
                # shell 启动失败多为 headless-shell 二进制缺失（playwright < 1.49），
                # 先回退完整 chromium 保住登录态；profile 若真损坏会在下一层被清理
                logger.warning(f"🧊 shell 内核启动失败: {e}，回退完整 chromium（保留登录态）...")
                self.profile = profile = "base"
                try:
                    self.context = await self._launch_context(user_data_dir, profile)
                    logger.info("🧊 已回退完整 chromium 内核启动成功")
                except Exception as e2:
                    self.context = await self._relaunch_after_cleanup(user_data_dir, profile, e2)
            else:
                self.context = await self._relaunch_after_cleanup(user_data_dir, profile, e)
        logger.info(f"浏览器启动完成 (headless={self.headless}, profile={self.profile})")

        # ---- 关闭 launch_persistent_context 自动创建的 about:blank 占位页 ----
        # 它会常驻一个独立 renderer 进程（实测 ~74-80MB），监控页全部用 new_page() 开
        await self._close_blank_pages()
        if self.headless:
            logger.info("🧊 空白占位页已清理（独立 renderer -80MB）")

    async def _launch_context(self, user_data_dir: Path, profile: str):
        cfg = self.config.browser
        launch_args = [
            # ---- A 级削内存参数（实测结论）----
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-gpu",                       # gpu-process(swiftshader) 实测 482MB，禁后归零
            "--disable-software-rasterizer",
            # 关站点隔离：合并 renderer，省内存
            "--disable-features=site-per-process,IsolateOrigins",
            "--process-per-site",                  # 同站合并渲染进程
            "--js-flags=--max-old-space-size=192", # V8 堆封顶，防 renderer 无限膨胀
            # ---- 二阶段压缩（实测稳定）----
            "--in-process-gpu",    # gpu 进程收编为 browser 内线程：-72MB
            # 注：playwright 默认已带 --disable-background-timer-throttling /
            #     --disable-renderer-backgrounding，页面定时器（push_count /
            #     feed_all_update）不会被节流
        ]
        channel = cfg.get('channel', 'chromium')
        if self.headless and profile == "shell":
            channel = "chromium-headless-shell"
        logger.info(f"🧊 内存优化 profile={profile}, channel={channel}")
        return await self.playwright.chromium.launch_persistent_context(
            str(user_data_dir),
            channel=channel,
            headless=self.headless,
            args=launch_args,
            viewport=cfg.get('viewport', {"width": 1280, "height": 720})
        )

    async def _relaunch_after_cleanup(self, user_data_dir: Path, profile: str, err):
        """最后兜底：清理 profile 后重试（会丢失登录态，需重新扫码）"""
        logger.warning(f"浏览器再次启动失败: {err}，清理 profile 后重试（登录态将丢失）...")
        shutil.rmtree(user_data_dir, ignore_errors=True)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        return await self._launch_context(user_data_dir, profile)

    async def _close_blank_pages(self):
        if not self.context:
            return
        keep = set()
        try:
            from .core.platform import names as _plat_names, attr as _plat_attr
            for pn in _plat_names():
                keep.update(_plat_attr(pn, 'domains') or ())
        except Exception:
            keep = set()
        if not keep:
            return
        for p in list(self.context.pages):
            try:
                if not any(d in (p.url or "") for d in keep):
                    await p.close()
            except Exception:
                pass

    async def stop(self):
        """限时收尾。

        Windows 下 Ctrl+C 会把整个控制台进程组的 playwright driver 一起杀掉，
        context.close() 可能永远等不到 RPC 应答——两段收尾都要限时。
        """
        if self.context:
            try:
                await asyncio.wait_for(self.context.close(), timeout=10)
            except Exception as e:
                logger.debug(f"关闭 context 失败/超时（驱动可能已退出）: {e}")
            self.context = None
        if self.playwright:
            try:
                await asyncio.wait_for(self.playwright.stop(), timeout=5)
            except Exception as e:
                logger.debug(f"关闭 playwright 失败/超时: {e}")
        logger.info("浏览器已关闭")

    async def restart(self):
        """每日维护：整体重启浏览器（context + playwright），各平台页随后
        由各自 relogin() 重新打开。seen 集合 / feed_cache / API 服务不动。"""
        logger.info("🔄 Hub 浏览器重启中...")
        await self.stop()
        await self.start()
        await self._close_blank_pages()
        logger.info("🔄 Hub 浏览器重启完成")

    # ========== 状态 ==========

    def pages_info(self) -> Dict[str, Any]:
        """面板展示用：context 与页面的存活信息"""
        if not self.context:
            return {"alive": False}
        try:
            pages = []
            for p in getattr(self.context, 'pages', []):
                try:
                    pages.append(p.url[:80])
                except Exception:
                    pass
            return {"alive": True, "pages": pages, "profile": self.profile}
        except Exception:
            return {"alive": True, "pages": [], "profile": self.profile}
