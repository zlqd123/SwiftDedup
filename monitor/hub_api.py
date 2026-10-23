# -*- coding: utf-8 -*-

"""统一 API 服务器 — 一个 aiohttp app 承载所有平台的路由与面板

路由命名：`/{平台前缀}/api/...`（由各平台插件注册具体路径），
RSS/Atom 同样带前缀：/{prefix}/rss/{id}、/{prefix}/atom/{id}。
 platforms.<name>.api.endpoints 可整体覆盖某平台的具体路径模板。

共享 /health 与根路径索引页（链接到各平台 dashboard）。
日志中间件对每个请求按路由前缀回写到对应平台的 dashboard 计数器。
"""

import asyncio
from datetime import datetime
from typing import Optional

from aiohttp import web
from loguru import logger


class HubAPIServer:
    """统一 HTTP 出口"""

    def __init__(self, config):
        self.config = config
        self.host = config.api.get('host', '127.0.0.1')
        self.port = int(config.api.get('port', 28801))
        self.external_base_url = getattr(config, 'external_base_url',
                                         f'http://{self.host}:{self.port}')
        self.app: Optional[web.Application] = None
        self.runner: Optional[web.AppRunner] = None
        self.services = []      # [PlatformBackend]，由 main 注册
        self.registrars = []    # 额外路由注册方（如 SDHub），回调 fn(app)
        self._log_silencer = config.logging.get('console_level', 'INFO') != 'DEBUG'

    def add_service(self, service):
        """service 需提供 .api（APIServer 实例，有 register_routes(app)）
        与可选 .dashboard（有 register(app, prefix)）与 .name"""
        self.services.append(service)

    def add_registrar(self, fn):
        """额外的路由注册回调（sd 聚合端点等）"""
        self.registrars.append(fn)

    async def start(self):
        self.app = web.Application(middlewares=[self._log_middleware])
        self.app.router.add_get('/health', self.health_check)
        self.app.router.add_get('/', self.index_page)
        # 注：/ui/ 门户由 monitor.console.Console 注册（避免重复路由）
        for svc in self.services:
            svc.api.register_routes(self.app)
        for fn in self.registrars:
            try:
                fn(self.app)
            except Exception as e:
                logger.exception(f"扩展路由注册失败: {e}")
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        routes = "、".join(f"/ui/{svc.name}/" for svc in self.services) + "、/ui/sd/"
        logger.info(f"API 服务启动: http://{self.host}:{self.port}（面板: {routes}）")

    async def stop(self):
        # 先主动断开各平台 dashboard 的 SSE 长连接（否则 runner.cleanup() 会被
        # events_stream 拖住，需先主动断开）
        for svc in self.services:
            dash = getattr(svc, 'dashboard', None)
            if dash and hasattr(dash, 'shutdown'):
                try:
                    dash.shutdown()
                except Exception:
                    pass
        if self.runner:
            try:
                await asyncio.wait_for(self.runner.cleanup(), timeout=8)
            except Exception as e:
                logger.debug(f"API runner 清理超时（遗留连接会被 OS 回收）: {e}")
        logger.info("API 服务已停止")

    # ---------- 中间件 ----------

    def _service_for_path(self, path: str):
        """按统一端点 scheme /{用途}/{平台}/... 解析所属平台（用途 data/api/rss/atom/ui）；
        兼容旧式 /{平台}/... 起点"""
        parts = [p for p in path.split('/') if p]
        if len(parts) >= 2:
            if parts[0] in ('data', 'api', 'rss', 'atom', 'ui') and len(parts) >= 3:
                name = parts[1]
            else:
                name = parts[0]
            for svc in self.services:
                if svc.name == name:
                    return svc
        return None

    @web.middleware
    async def _log_middleware(self, request, handler):
        svc = self._service_for_path(request.path)
        dash = getattr(svc, 'dashboard', None) if svc else None
        prefix = f"/data/{svc.name}" if svc else ""
        # 面板自身的轮询不产生日志噪音（但仍然计数 —— API 流量表要能看见它）
        quiet = request.path in (f"/ui/{svc.name}/stats" if svc else None,
                                 f"/ui/sd/stats",
                                 f"{prefix}/api/stats" if svc else None)
        if not quiet:
            # 所有 API 请求行一律 DEBUG：文件/网页面板保留，控制台按 console_level 门限自然隐藏
            logger.debug(f"[API] {request.method} {request.path}")
        if dash:
            dash.note_request(request.method, request.path)
        resp = await handler(request)
        if dash and getattr(resp, 'status', 200) >= 400:
            dash.note_fail(request.method, request.path)
        return resp

    # ---------- 页面 ----------

    async def health_check(self, request):
        out = {'status': 'ok', 'timestamp': datetime.now().isoformat()}
        sd = getattr(self, '_sd_engine', None)
        if sd is not None:
            try:
                out['dedup'] = sd.status()
            except Exception:
                pass
        return web.json_response(out)

    async def index_page(self, request):
        """根路径：跳到控制台门户（/ui/）"""
        raise web.HTTPFound('/ui/')
