# -*- coding: utf-8 -*-

"""网页控制台 — 门户 / 登录扫码 / 配置子页（Phase 2 收尾）

启动后自动打开浏览器到控制台；控制台是"一切操作的入口"：

    /ui/            门户（子页面超链接：登录 / 平台 / 信源 / 配置）
    /ui/login/      登录页：显示两平台二维码（网页直接扫码）+ 重新获取 + 状态
    /ui/config/     配置页：config.yaml 全部配置项（可编辑保存，重启生效）
    /ui/sd/         信源面板（判重/信源/抓取名单，见 sd_api.py）

登录流程要点（Phase 2 网页化）：
- 平台服务在登录失败时**保留路由**，控制台始终可用；
- 登录流程把二维码落到 data/<平台>/qrcode.png，控制台按需读取并轮询刷新；
- "重新获取二维码" = 关掉旧页面重跑登录流程（平台服务 retry_login）。
"""

import json
import os
import shutil

from .core.platform import (names as _plat_names, has as _plat_has,
                            attr as _plat_attr, display_name as _plat_display)

from .core.platform import (names as _plat_names, has as _plat_has,
                            attr as _plat_attr, display_name as _plat_display)
import asyncio
import time
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from aiohttp import web
from loguru import logger


class Console:
    """控制台（注册到统一 app）"""

    def __init__(self, config, engine=None, registry=None):
        self.config = config
        self.engine = engine
        self.registry = registry
        self.services: Dict[str, object] = {}   # platform -> Service（main 注入）
        self._hub_started = time.time()

    # ---------- 注册 ----------

    def _render(self, html: str) -> str:
        """页面渲染注入：顶栏平台项/门户卡片/登录页平台表/配置页平台标签"""
        nav = ('<a href="/ui/">门户</a><a href="/ui/login/">登录扫码</a>'
               '<a href="/ui/sd/">信源管理</a><a href="/ui/events/">信源面板</a>'
               + ''.join(f'<a href="/ui/{n}/">{_plat_display(n)}面板</a>'
                         for n in _plat_names())
               + '<a href="/ui/opener/">操作账号</a><a href="/ui/config/">配置</a>')
        cards = ''.join(f' ["{_plat_display(n)}面板","/ui/{n}/","{_plat_display(n)}侧实时监控"],'
                        for n in _plat_names())
        ps = '[' + ','.join(f'["{n}","{_plat_display(n)}"]' for n in _plat_names()) + ']'
        labels = json.dumps({n: _plat_display(n) for n in _plat_names()},
                            ensure_ascii=False)
        return (html.replace('__NAV__', nav).replace('__PLAT_CARDS__', cards)
                    .replace('__PS__', ps).replace('__PLAT_LABELS__', labels))


    def register(self, app: web.Application):
        r = app.router
        r.add_get('/ui/', self.portal_page)
        r.add_get('/ui/login/', self.login_page)
        r.add_get('/ui/config/', self.config_page)
        r.add_get('/api/login/status', self.api_login_status)
        r.add_post('/api/login/{platform}', self.api_login_trigger)
        r.add_get('/data/login/qrcode/{platform}', self.qrcode_image)
        r.add_get('/ui/opener/', self.opener_page)
        r.add_get('/ui/opener/{platform}', self.manual_browser)
        r.add_get('/api/config', self.api_config_get)
        r.add_post('/api/config', self.api_config_save)
        logger.info("[控制台] 门户/登录/配置端点已注册")

    # ---------- 门户 ----------

    async def portal_page(self, request):
        return web.Response(text=self._render(_PORTAL_HTML), content_type='text/html', charset='utf-8')

    # ---------- 登录 ----------

    async def login_page(self, request):
        return web.Response(text=self._render(_LOGIN_HTML), content_type='text/html', charset='utf-8')

    def _platform_state(self, platform: str) -> dict:
        svc = self.services.get(platform)
        if svc is None:
            return {'platform': platform, 'state': 'disabled', 'message': '平台未启用',
                    'qr': False, 'qr_v': '', 'at': 0}
        try:
            st = svc.login_status()
        except Exception as e:
            st = {'platform': platform, 'state': 'error', 'message': str(e), 'qr': False}
        st.setdefault('platform', platform)
        st.setdefault('state', 'idle')
        st.setdefault('qr', False)
        st['qr_v'] = self._qr_version(platform)
        return st

    @staticmethod
    def _qr_version(platform: str) -> str:
        """二维码文件版本号（mtime）—— 前端据此判断是否需要换图，避免闪烁"""
        try:
            p = Path('./data') / platform / 'qrcode.png'
            if p.exists():
                return str(int(p.stat().st_mtime))
        except Exception:
            pass
        return ''

    async def api_login_status(self, request):
        data = {p: self._platform_state(p) for p in _plat_names()}
        data['hub'] = {'started_at': self._hub_started}
        return web.json_response(data)

    async def api_login_trigger(self, request):
        platform = request.match_info['platform']
        if not _plat_has(platform):
            return web.json_response({'error': 'unknown platform'}, status=400)
        svc = self.services.get(platform)
        if svc is None:
            return web.json_response({'error': '平台未启用'}, status=400)
        try:
            started = await svc.retry_login()
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
        return web.json_response({'started': bool(started),
                                  'state': self._platform_state(platform)})

    async def qrcode_image(self, request):
        """二维码图片（网页直接扫码）；{platform}_debug 取调试截图"""
        platform = request.match_info['platform']
        debug = platform.endswith('_debug')
        if debug:
            platform = platform[:-5]
        if not _plat_has(platform):
            return web.json_response({'error': 'unknown platform'}, status=404)
        name = 'qrcode_debug.png' if debug else 'qrcode.png'
        path = Path('./data') / platform / name
        if not path.exists():
            return web.Response(status=404, text='not found')
        return web.Response(body=path.read_bytes(), content_type='image/png',
                            headers={'Cache-Control': 'no-store'})

    # ---------- 操作账号（复用 Cookie 的有头浏览器） ----------

    @staticmethod
    def _manual_url(platform: str) -> str:
        return _plat_attr(platform, 'manual_url') or ''

    def _manual_page(self, title: str, body: str):
        html = ('<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
                '<title>SwiftDedup · 操作账号</title><style>' + _COMMON_CSS + '</style></head><body>'
                '<header><b>操作账号</b>' + self._render('__NAV__') + '</header><main>'
                '<div class="card"><h2>' + title + '</h2><p>' + body + '</p>'
                '<p class="mute">说明：浏览器复用主程序的登录 Cookie；'
                '手动搜索账号后，到「信源管理→抓取名单」按 uid 添加监控。'
                '关掉浏览器窗口即退出，不影响主程序监控页。</p></div></main></body></html>')
        return web.Response(text=html, content_type='text/html', charset='utf-8')

    async def opener_page(self, request):
        body = ''.join(
            '<a class="navbtn" href="/ui/opener/' + n + '">打开'
            + _plat_display(n) + '账号操作</a>' for n in _plat_names())
        return self._manual_page('选择平台', body)

    async def manual_browser(self, request):
        platform = str(request.match_info['platform']).lower()
        open_url = self._manual_url(platform)
        if open_url is None:
            return web.Response(status=404, text='unknown platform')
        svc = self.services.get(platform)
        hub = getattr(getattr(svc, 'bm', None), 'hub', None)
        if hub is None or getattr(hub, 'context', None) is None:
            return self._manual_page('暂无法打开',
                '主浏览器尚未就绪，同域 Cookie 不可读取——等登录流程完成后再来。')
        try:
            cookies = await hub.context.cookies([open_url])
        except Exception as e:
            return self._manual_page('读取 Cookie 失败', str(e))
        if not cookies:
            return self._manual_page('没有可复用的登录 Cookie',
                '请先在「登录扫码」完成该平台登录，再来操作账号。')
        asyncio.create_task(self._launch_manual(platform, cookies))
        return self._manual_page('浏览器启动中…',
            f'正在有头模式打开 {open_url}（复用 {len(cookies)} 个 Cookie）。'
            '窗口未弹出时查看主控制台日志。')

    async def _launch_manual(self, platform: str, cookies: list):
        try:
            prev = getattr(self, '_manual_refs', {}).pop(platform, None)
            if prev:
                try:
                    await prev[1].close(); await prev[0].stop()
                except Exception:
                    pass
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            ch = self.config.browser.get('channel', 'chromium')
            if ch == 'chromium-headless-shell':
                ch = 'chromium'          # 有头必须完整内核
            browser = await pw.chromium.launch(channel=ch, headless=False)
            ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
            await ctx.add_cookies(cookies)
            page = await ctx.new_page()
            await page.goto(open_url, wait_until='domcontentloaded')
            if not hasattr(self, '_manual_refs'):
                self._manual_refs = {}
            self._manual_refs[platform] = (pw, browser)
            def _gone(*_):
                try: self._manual_refs.pop(platform, None)
                except Exception: pass
            browser.on('disconnected', _gone)
            logger.info(f"[操作账号] 已打开 {platform} 有头浏览器: {open_url}")
        except Exception as e:
            logger.error(f"[操作账号] {platform} 启动失败: {e}")


    # ---------- 配置页 ----------

    async def config_page(self, request):
        return web.Response(text=self._render(_CONFIG_HTML), content_type='text/html', charset='utf-8')

    def _config_path(self) -> Path:
        return Path(getattr(self.config, 'config_path', 'config.yaml'))

    async def api_config_get(self, request):
        path = self._config_path()
        try:
            data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        except Exception as e:
            return web.json_response({'error': f'读取配置失败: {e}'}, status=500)
        return web.json_response({'config': data, 'path': str(path),
                                  'hint': '多数项需重启进程生效；dedup.mode 等少数项可热改'})

    async def api_config_save(self, request):
        """保存配置：整体树按 JSON 覆盖写入（先备份 .bak）"""
        body = await request.json()
        new_cfg = body.get('config')
        if not isinstance(new_cfg, dict):
            return web.json_response({'error': 'config must be an object'}, status=400)
        path = self._config_path()
        try:
            current = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        except Exception as e:
            return web.json_response({'error': f'读取现有配置失败: {e}'}, status=500)
        merged = _deep_merge(current, new_cfg)
        try:
            if path.exists():
                shutil.copy2(path, path.with_suffix(path.suffix + '.bak'))
            path.write_text(yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
                            encoding='utf-8')
        except Exception as e:
            return web.json_response({'error': f'写入失败: {e}'}, status=500)
        logger.info(f"[控制台] 配置已保存: {path}（备份 {path.name}.bak）")
        return web.json_response({'saved': True, 'path': str(path),
                                  'backup': str(path.with_suffix(path.suffix + '.bak'))})

    # ---------- 启动后提示访问地址 ----------

    def open_browser(self):
        """启动后**不自动开浏览器**，只把控制台地址打出来（避免抢焦点/被浏览器拦）"""
        if not bool(self.config.api.get('open_browser', False)):
            host = self.config.api.get('host', '127.0.0.1')
            port = self.config.api.get('port', 28801)
            if host in ('0.0.0.0', '::'):
                host = '127.0.0.1'
            url = f"http://{host}:{port}/ui/"
            logger.info("=" * 56)
            logger.info(f"  控制台已就绪，请在浏览器打开： {url}")
            logger.info(f"  登录扫码： {url}login/    配置： {url}config/")
            logger.info("=" * 56)
            return
        host = self.config.api.get('host', '127.0.0.1')
        port = self.config.api.get('port', 28801)
        if host in ('0.0.0.0', '::'):
            host = '127.0.0.1'
        url = f"http://{host}:{port}/ui/"
        try:
            webbrowser.open(url)
            logger.info(f"[控制台] 已打开浏览器: {url}")
        except Exception as e:
            logger.warning(f"[控制台] 自动打开浏览器失败（请手动访问 {url}）: {e}")


def _deep_merge(base: dict, patch: dict) -> dict:
    """字典深合并（列表整体替换）"""
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ============================================================================
# 页面
# ============================================================================

# 统一顶栏：所有子页面（含监控面板）都保持入口一致（平台项由 _render 注入）
_COMMON_CSS = """
 body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#f5f6f8;color:#1e2430;font-size:20px}
 input,select,button,textarea{font-size:20px}
 header{background:#1f2833;color:#fff;padding:12px 20px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
 header b{font-size:20px}
 header a{color:#9dc;opacity:.9;text-decoration:none;margin-right:14px}
 header a:hover{opacity:1;text-decoration:underline}
 main{max-width:1080px;margin:22px auto;padding:0 16px}
 .card{background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:16px;margin-bottom:16px}
 h2{margin:0 0 10px;font-size:20px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
 .tile{background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:16px}
 .tile h3{margin:0 0 6px;font-size:19px}
 .tile p{margin:4px 0;color:#667;font-size:17px;line-height:1.5}
 .pill{display:inline-block;padding:2px 10px;border-radius:12px;background:#eef;color:#335;font-size:16px}
 .ok{background:#e6ffe6;color:#282}.bad{background:#ffe9e4;color:#a33}.wait{background:#fff6e0;color:#886}
 .mute{color:#889;font-size:16px}
 button{padding:6px 14px;border:0;border-radius:6px;background:#356;color:#fff;cursor:pointer}
 button.ghost{background:#e8ecf2;color:#334}
 .navbtn{display:inline-block;padding:10px 20px;border-radius:8px;background:#2b517c;color:#fff;text-decoration:none;font-size:18px}
 input,select{padding:5px 8px;border:1px solid #ccd;border-radius:6px;margin-right:6px}
 table{width:100%;border-collapse:collapse;font-size:17px}
 td,th{padding:5px 8px;border-bottom:1px solid #eef0f3;text-align:left;vertical-align:top}
 .path{font-family:Consolas,monospace;font-size:16px;color:#556;word-break:break-all}
"""

_PORTAL_HTML = ('<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>SwiftDedup 控制台</title><style>' + _COMMON_CSS + '</style></head><body>'
                '<header><b>SwiftDedup 控制台</b>' + '__NAV__' + '</header><main>'
                '<div class="grid" id="tiles"></div>'
                '</main><script>'
                'const CARDS=['
                ' ["登录扫码","/ui/login/","各平台二维码登录、状态与重试"],'
                ' ["信源管理","/ui/sd/","信源编排、判重日志、抓取名单"],'
                ' ["信源面板","/ui/events/","登录/判重/平台事件实时流"],'
                ' ["操作账号","/ui/opener/","复用 Cookie 弹出有头浏览器手动加号"],'
                '__PLAT_CARDS__'
                ' ["配置","/ui/config/","config.yaml 全部配置项（可编辑保存）"],'
                '];'
                'document.getElementById("tiles").innerHTML=CARDS.map(c=>'
                '`<div class="tile"><h3><a href="${c[1]}">${c[0]}</a></h3><p>${c[2]}</p></div>`).join("");'
                'fetch("/api/login/status").then(r=>r.json()).then(d=>{'
                ' const el=document.createElement("div");el.className="card";'
                ' el.innerHTML="<h2>平台状态</h2>"+Object.entries(d).filter(([k])=>k!=="hub")'
                ' .map(([k,v])=>`<div>${k}：<span class="pill ${v.state==="logged_in"?"ok":(v.state==="failed"?"bad":"wait")}">'
                '${v.state}</span> <span class="mute">${v.message||""}</span></div>`).join("");'
                ' document.querySelector("main").prepend(el);});'
                '</script></body></html>')

_LOGIN_HTML = ('<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width,initial-scale=1">'
               '<title>SwiftDedup · 登录扫码</title><style>' + _COMMON_CSS + '</style></head><body>'
               '<header><b>登录扫码</b>' + '__NAV__' + '</header><main>'
               '<div class="card"><h2>说明</h2><p class="mute">'
               '启动后若平台未登录，这里会出现二维码，用对应 APP 直接扫码即可；'
               '二维码过期点"重新获取二维码"。已登录的平台会自动显示"已登录"。</p></div>'
               '<div class="grid" id="cards"></div></main><script>'
               'const PS=__PS__;'
               'const CACHE={};'
               'function card(p,name){const st=(window.S||{})[p]||{};'
               ' const sig=(st.state||"")+"|"+(st.message||"")+"|"+(st.qr?"1":"0")+"|"+(st.qr_v||"");'
               ' if(CACHE[p] && CACHE[p].sig===sig) return CACHE[p].html;'
               ' const cls=st.state==="logged_in"?"ok":(st.state==="failed"?"bad":"wait");'
               ' const img=st.qr?`<img src="/data/login/qrcode/${p}?v=${st.qr_v}" width="220" '
               ' onerror="this.style.display=\'none\'">`:`<div class="mute">（暂无二维码，等待平台登录流程…）</div>`;'
               ' const dbg=`<a class="pill" style="margin-left:6px" href="/data/login/qrcode/${p}_debug" '
               ' target="_blank">调试截图</a>`;'
               ' const html=`<div class="tile"><h3>${name}</h3>'
               ' <div><span class="pill ${cls}">${st.state||"idle"}</span> '
               ' <span class="mute">${st.message||""}</span></div>'
               ' <div style="margin:10px 0">${img}</div>'
               ' <button onclick="relogin(\'${p}\')">重新获取二维码</button>'
               ' <a class="pill" style="margin-left:6px" href="/data/login/qrcode/${p}" target="_blank">二维码原图</a>'
               ' ${dbg}'
               ' </div>`;'
               ' CACHE[p]={sig:sig,html:html};return html;}'
               'async function relogin(p){await fetch(`/api/login/${p}`,{method:"POST"});setTimeout(load,1500);}'
               'async function load(){const d=await (await fetch("/api/login/status")).json();'
               ' window.S=d;'
               ' document.getElementById("cards").innerHTML=PS.map(p=>card(p[0],p[1])).join("");}'
               'load();setInterval(load,3000);'
               '</script></body></html>')

_CONFIG_HTML = ('<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>SwiftDedup · 配置</title><style>' + _COMMON_CSS + '</style></head><body>'
                '<header><b>配置</b>' + '__NAV__' + '</header><main>'
                '<div class="card"><h2>配置文件</h2>'
                '<div class="path" id="path"></div>'
                '<p class="mute">保存前自动备份为 .bak；多数配置项需重启进程后生效'
                '（判重模式等少数项可热改）。</p>'
                '<button onclick="save()">保存全部配置</button> '
                '<button class="ghost" onclick="load()">重新载入</button> '
                '<span id="msg" class="mute"></span></div>'
                '<div id="sections"></div>'
                '</main><script>'
                'let CFG={};'
                'const PLAT_CN_LABELS=__PLAT_LABELS__;'
                'const LABELS={'
                ' api:"接口服务",port:"端口",host:"监听地址",external_base_url:"对外基址",'
                ' dashboard:"启用网页面板",open_browser:"启动后自动打开浏览器",'
                ' browser:"浏览器",channel:"内核通道",viewport:"视口",user_data_dir:"登录态目录",'
                ' logging:"日志",console_level:"控制台日志级别",file_level:"文件日志级别",'
                ' file:"日志文件",backup_days:"日志保留天数",'
                ' maintenance:"每日维护",browser_restart:"浏览器重启",'
                ' dedup:"判重与信源",mode:"判重模式",fuzzy_threshold:"模糊相似度阈值",'
                ' window_hours:"模糊判重窗口(小时)",near_miss_low:"近失下限",'
                ' tail_note:"正文尾注",sources_file:"信源映射文件",data_dir:"判重账本目录",'
                ' seeds:"历史播种路径",platform:"平台",path:"路径",'
                ' platforms:"平台配置",monitor:"监控",storage:"存储",'
                ' watchlist:"关注名单",content_filter:"广告模板过滤",temp_user_ids:"占位用户",'
                ' enabled:"启用",check_interval:"检查间隔(秒)",badge_wait:"徽章等待(秒)",'
                ' badge_confirm:"徽章确认(秒)",watchdog_interval:"看门狗间隔(秒)",'
                ' watchdog_cooldown:"看门狗冷却(秒)",cache_dir:"缓存目录",max_per_user:"每用户上限",'
                ' auto_subscribe:"查询即订阅",history_limit:"历史统计条数",'
                ' repeat_threshold:"重复行阈值",recover_urls:"短链找回",'
                ' max_templates:"模板版本上限",state_file:"过滤状态文件",'
                ' development:"开发模式",'
                ' window_start:"窗口开始(时)",window_end:"窗口结束(时)",guard_minutes:"护栏(分钟)",'
                ' dashboard_level:"面板日志级别",width:"宽",height:"高",...PLAT_CN_LABELS,'
                '};'
                'const ALIAS={"shadow":"影子(只记账)","enforce":"强制(重复静默)",'
                '"chromium":"完整内核","chromium-headless-shell":"精简内核",'
                '"INFO":"信息","DEBUG":"调试","WARNING":"警告","ERROR":"错误"};'
                'const CHOICES={"dedup.mode":["shadow","enforce"],'
                ' "logging.console_level":["INFO","DEBUG","WARNING","ERROR"],'
                ' "logging.file_level":["INFO","DEBUG","WARNING","ERROR"],'
                ' "logging.dashboard_level":["INFO","DEBUG","WARNING","ERROR"],'
                ' "browser.channel":["chromium","chromium-headless-shell"]};'
                'function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")'
                '.replace(/>/g,"&gt;").replace(/"/g,"&quot;");}'
                'const PLBL={"watchlist.file":"名单路径"};'
                'function label(k,path){return PLBL[path]||LABELS[k]||k;}'
                'function alias(v){return ALIAS[v]||v;}'
                'function leaves(obj,path,out){'
                '  for(const [k,v] of Object.entries(obj||{})){'
                '    const p=path?path+"."+k:k;'
                '    if(v&&typeof v==="object"&&!Array.isArray(v)){leaves(v,p,out);}'
                '    else{const g0=path.split(".")[0];out.push({path:p,key:k,val:v,group:g0||k});}}}'
                'function render(){'
                '  const out=[];leaves(CFG,"",out);'
                '  const groups={};out.forEach(o=>{(groups[o.group]=groups[o.group]||[]).push(o);});'
                '  const rowHtml=o=>{'
                '      const p=esc(o.path),v=o.val;'
                '      let ctrl;'
                '      if(typeof v==="boolean"){'
                '        ctrl=`<label class="sw"><input type="checkbox" data-p="${p}" ${v?"checked":""}> 启用</label>`;}'
                '      else if(typeof v==="number"){'
                '        ctrl=`<input class="inp" type="number" data-p="${p}" value="${v}">`;}'
                '      else if(typeof v==="string"){'
                '        const ch=CHOICES[o.path];'
                '        ctrl=ch?`<select class="inp" data-p="${p}">${ch.map(c=>'
                '              `<option value="${c}" ${c===v?"selected":""}>${esc(alias(c))}</option>`).join("")}</select>`'
                '              :`<input class="inp" type="text" data-p="${p}" value="${esc(v)}">`;}'
                '      else{'
                '        ctrl=`<textarea class="inp" data-p="${p}" data-json="1" rows="2"'
                "              placeholder='JSON 数组/对象'>${esc(JSON.stringify(v))}</textarea>`;}"
                '      return `<tr><td class="k">${esc(label(o.key,o.path))}</td>'
                '        <td class="path">${p}</td><td class="v">${ctrl}</td></tr>`;};'
                '  document.getElementById("sections").innerHTML=Object.entries(groups).map(([g,items])=>{'
                '    if(g==="platforms"){const sub={};'
                '      items.forEach(o=>{const s=o.path.split(".")[1]||"(平台)";(sub[s]=sub[s]||[]).push(o);});'
                '      return Object.entries(sub).map(([s2,its])=>'
                '        `<div class="card"><h2>${esc(label(g))} · ${esc(label(s2))} '
                '        <span class="mute">${esc(g+"."+s2)}</span></h2>'
                '        <table class="cfg"><tbody>${its.map(rowHtml).join("")}</tbody></table></div>`).join("");}'
                '    const rows=items.map(rowHtml).join("");'
                '    return `<div class="card"><h2>${esc(label(g))} <span class="mute">${esc(g)}</span></h2>'
                '      <table class="cfg"><tbody>${rows}</tbody></table></div>`;}).join("");}'
                'function setPath(obj,path,val){const ks=path.split(".");let o=obj;'
                '  for(let i=0;i<ks.length-1;i++){if(typeof o[ks[i]]!=="object"||o[ks[i]]===null)o[ks[i]]={};'
                '    o=o[ks[i]];}o[ks[ks.length-1]]=val;}'

                'async function load(){const d=await (await fetch("/api/config")).json();'
                '  if(d.error){document.getElementById("msg").textContent=d.error;return;}'
                '  CFG=d.config;document.getElementById("path").textContent="文件："+d.path;'
                '  render();}'
                'async function save(){const inputs=document.querySelectorAll("[data-p]");'
                '  const out=JSON.parse(JSON.stringify(CFG));'
                '  inputs.forEach(el=>{const p=el.getAttribute("data-p");let v;'
                '    if(el.getAttribute("data-json")){try{v=JSON.parse(el.value);}catch(e){v=el.value;}}'
                '    else if(el.type==="checkbox"){v=el.checked;}'
                '    else if(el.type==="number"){v=Number(el.value);}'
                '    else{v=el.value;}'
                '    setPath(out,p,v);});'
                '  const r=await (await fetch("/api/config",{method:"POST",'
                '    headers:{"Content-Type":"application/json"},body:JSON.stringify({config:out})})).json();'
                '  document.getElementById("msg").textContent=r.error?("失败："+r.error)'
                '    :("已保存（备份 "+r.backup+"），重启后生效");}'
                'load();'
                '</script><style>'
                'table.cfg{width:100%;border-collapse:collapse;font-size:17px;table-layout:fixed}'
                'table.cfg td{padding:6px 8px;border-bottom:1px solid #eef0f3;vertical-align:middle}'
                'td.k{width:24%;font-weight:500}td.path{width:38%;color:#667;'
                'font-family:Consolas,monospace;font-size:16px;word-break:break-all}'
                'td.v{width:38%}.inp{width:100%;box-sizing:border-box;padding:5px 8px;'
                'border:1px solid #ccd;border-radius:6px}.sw{font-size:17px;color:#445}'
                '</style></body></html>')
