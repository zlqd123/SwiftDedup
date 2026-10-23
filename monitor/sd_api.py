# -*- coding: utf-8 -*-

"""sd 端点 + 尾注 + 面板（docs/phase2_spec.md §5 / §6 / §9）

对外职责：
- /data/sd/...      聚合查询（先到为主）：sources / source/{sid} / posts / post / alias 反查
- /rss/sd/{sid} 等  信源订阅（canonical 单流），正文尾部拼接"来自{平台}"（D10）
- /api/sd/...       管理（mode / seed / release / source CRUD / 判重日志）
- /ui/sd/...        信源面板（管理聚合 id / 判重日志 / 手动放行 / 状态）

尾注规则（D10）：仅 sd 系下发端点拼接；磁盘缓存永远不改写；
手动放行的内容走同一下发路径，天然不叠加。
"""

import html as _html
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

from aiohttp import web
from loguru import logger

from .dedup.engine import pad_sid

from .core.platform import (names as _plat_names, has as _plat_has,
                            attr as _plat_attr, display_name as _plat_display)
import json as _json


def _img_urls(platform, post, base) -> List[str]:
    """管理页图片地址：构造在平台插件的 image_urls hook"""
    fn = _plat_attr(platform, 'image_urls')
    if fn is None:
        return [str(x) for x in (post or {}).get('images') or []]
    try:
        return list(fn(post or {}, base))
    except Exception as e:
        logger.warning(f'[sd] 图片地址构造失败 {platform}: {e!r}')
        return []


def _forward_note(platform, post) -> str:
    """转发帖补引摘（无则空串）"""
    fn = _plat_attr(platform, 'forward_note')
    if fn is None:
        return ''
    try:
        return fn(post or {}) or ''
    except Exception as e:
        logger.warning(f'[sd] 转发补引失败 {platform}: {e!r}')
        return ''


def _post_link(platform, post, pid: str):
    """发帖外链与作者名：构造在平台插件的 post_link hook"""
    post = post or {}
    fn = _plat_attr(platform, 'post_link')
    if fn is not None:
        try:
            link, author = fn('', post)
            return link or '', author or ''
        except Exception as e:
            logger.warning(f'[sd] 外链构造失败 {platform}: {e!r}')
    return post.get('url') or '', post.get('author') or ''


from .dedup import fingerprint as FP


class SDHub:
    """聚合层 HTTP 门面（注册到统一 app）"""

    def __init__(self, config, engine, registry):
        self.config = config
        self.engine = engine
        self.registry = registry
        self.ledger = engine.ledger
        self.services: Dict[str, object] = {}   # platform -> Service，main 注入
        self.seeder = None                      # Seeder，main 注入
        self.external_base_url = getattr(
            config, 'external_base_url',
            f"http://{config.api.get('host','127.0.0.1')}:{config.api.get('port','28801')}")
        self._sse_clients: List = []
        self.tail_note_on = bool((config.dedup or {}).get('tail_note', True))

    # ---------- 注册 ----------

    def register(self, app: web.Application):
        r = app.router
        # data —— 聚合查询（先到为主）
        r.add_get('/data/sd/sources', self.list_sources)
        r.add_get('/data/sd/source/{sid}', self.source_info)
        r.add_get('/data/sd/source/{sid}/posts', self.source_posts)
        r.add_get('/data/sd/post/{aid}', self.post_detail)
        r.add_get('/data/sd/alias/{platform}/{pid}', self.alias_lookup)
        r.add_get('/data/sd/alias/user/{platform}/{uid}', self.alias_user)
        # 订阅
        r.add_get('/rss/sd/{sid}', self.feed)
        r.add_get('/atom/sd/{sid}', self.feed)
        # api —— 管理
        r.add_get('/api/sd/status', self.api_status)
        r.add_post('/api/sd/mode', self.api_mode)
        r.add_post('/api/sd/seed', self.api_seed)
        r.add_get('/api/sd/log/dedup', self.api_log)
        r.add_post('/api/sd/release', self.api_release)
        r.add_post('/api/sd/source', self.api_source_create)
        r.add_patch('/api/sd/source/{sid}', self.api_source_patch)
        r.add_delete('/api/sd/source/{sid}/account/{platform}/{uid}', self.api_source_unlink)
        # 抓取名单（平台 watchlist）：面板增删账号
        r.add_get('/api/sd/watchlist', self.api_watchlist)
        r.add_post('/api/sd/watchlist', self.api_watchlist_add)
        r.add_delete('/api/sd/watchlist/{platform}/{uid}', self.api_watchlist_del)
        r.add_post('/api/sd/link', self.api_link)
        r.add_delete('/api/sd/source/{sid}', self.api_source_delete)
        # ui —— 面板
        r.add_get('/api/sd/nicknames', self.api_nicknames)
        r.add_post('/api/sd/nickname', self.api_nickname_save)
        r.add_get('/ui/sd/', self.dashboard_page)
        r.add_get('/ui/events/', self.events_page)
        r.add_get('/ui/sd/stats', self.dashboard_stats)
        r.add_get('/ui/sd/events', self.dashboard_events)
        # 按需二维码卡片：读登录流程落盘的 data/{platform}/qrcode.png（不常驻）
        r.add_get('/data/sd/qrcode/{platform}', self.qrcode_image)
        logger.info("[sd] 聚合端点已注册（23 条）")

    # ---------- 帖子取数与渲染 ----------

    async def _load_post(self, platform: str, uid: str, pid: str) -> Optional[dict]:
        svc = self.services.get(platform)
        if not svc or not getattr(svc, 'storage', None):
            return None
        try:
            return await svc.storage.load_post(uid, pid)
        except Exception:
            return None

    @staticmethod
    def _platform_text(platform: str, post: Optional[dict], fallback: str) -> str:
        if post:
            fn = _plat_attr(platform, 'display_text')
            t = fn(post) if fn is not None else (post.get('content') or post.get('title') or '')
            if t.strip():
                return t
        return fallback or ''

    def _tail_note(self, platform: str, text: str) -> str:
        if not self.tail_note_on:
            return text
        note = f'来自{PLATFORM_NAME.get(platform, platform)}'
        if (text or '').rstrip().endswith(note):
            return text                                    # 幂等：不叠加（D10）
        return f"{(text or '').rstrip()}\n{note}"

    async def _render_item(self, rec: dict, *, with_tail: bool = True) -> Optional[dict]:
        platform, pid, uid = rec['platform'], str(rec['pid']), rec.get('uid')
        svc = self.services.get(platform)
        # canonical：等先到的平台补完图片再让下游可见（D4）
        if rec.get('role') == 'canonical' and svc is not None and \
                pid in set(getattr(svc, 'pending_images', None) or ()):
            return None
        post = await self._load_post(platform, uid, pid)
        text = self._platform_text(platform, post,
                                   '（原始帖已过平台缓存保留期，仅存链接）')
        # 图片地址与转发补引：由平台插件构造
        imgs = _img_urls(platform, post, self.external_base_url)
        fnote = _forward_note(platform, post)
        if fnote:
            text = f'{text}\n{fnote}'
        if with_tail:
            # 尾注按"本内容选取的信源=帖所在平台"标注；canonical 与
            # 手动放行的 alias 同路径，天然不叠加（D10 幂等判断在 _tail_note 内）
            text = self._tail_note(platform, text)
        # 链接（canonical 首发平台链接；released 别名则自身链接）
        link, author = _post_link(platform, post, pid)
        dt = datetime.fromtimestamp(rec.get('t') or time.time(), tz=timezone(timedelta(hours=8)))
        return {
            'aid': rec.get('aid') or '',
            'sid': rec.get('sid') or '',
            'platform': platform,
            'platform_name': PLATFORM_NAME.get(platform, platform),
            'pid': pid, 'uid': uid,
            'author': author,
            'text': text,
            'images': imgs,
            'link': link,
            'ts': rec.get('t'),
            'pubDate': dt.strftime('%a, %d %b %Y %H:%M:%S +0800'),
            'published': dt.isoformat(),
            'role': rec.get('role'),
            'released': bool(rec.get('released')),
        }

    # ---------- data：聚合查询 ----------

    async def list_sources(self, request):
        out = []
        for s in self.registry.all_sources():
            counts = self.ledger.counter_status(s['sid'])
            canon = sum(1 for rec in self.ledger.alias.values()
                        if rec.get('sid') == str(s['sid']) and rec.get('role') == 'canonical')
            out.append({**s, 'post_count': canon, 'counters': counts})
        return web.json_response({'sources': out, 'unclaimed': self.registry.unclaimed()})

    async def source_info(self, request):
        sid = request.match_info['sid']
        rec = self.registry.source(int(sid))
        if not rec:
            return web.json_response({'error': 'source not found'}, status=404)
        canon = [r for r in self.ledger.alias.values()
                 if r.get('sid') == str(sid) and r.get('role') == 'canonical']
        aliases = [r for r in self.ledger.alias.values()
                   if r.get('sid') == str(sid) and r.get('role') == 'alias']
        return web.json_response({'source': rec,
                                  'post_count': len(canon), 'alias_count': len(aliases),
                                  'counters': self.ledger.counter_status(sid)})

    async def source_posts(self, request):
        try:
            sid = int(request.match_info['sid'])
        except ValueError:
            return web.json_response({'error': 'bad sid'}, status=400)
        if not self.registry.has_sid(sid):
            return web.json_response({'error': 'source not found'}, status=404)
        limit = self._int(request, 'limit', 20, 1, 100)
        before = self._float(request, 'before', None)
        recs = self.ledger.timeline_for_sid(sid, limit=limit + 1, before_ts=before)
        items = []
        for rec in recs[:limit]:
            item = await self._render_item(rec)
            if item:
                items.append(item)
        more = (len(recs) > limit and
                (recs[limit - 1].get('t') if len(recs) > limit else None))
        return web.json_response({'sid': sid, 'items': items,
                                  'next_before': recs[limit - 1].get('t') if len(recs) > limit else None,
                                  'has_more': len(recs) > limit})

    async def post_detail(self, request):
        aid = request.match_info['aid']
        rec = self.engine.lookup_by_aid(aid)     # D19：一帖多聚合id均可反查
        if not rec:
            return web.json_response({'error': 'post not found'}, status=404)
        item = await self._render_item(rec, with_tail=False)
        aliases = []
        for a in self.ledger.alias.values():
            if (a.get('dup_of') or {}).get('aid') == aid:
                aliases.append({'platform': a['platform'], 'pid': a['pid'],
                                'uid': a.get('uid'), 'ts': a.get('t'),
                                'released': bool(a.get('released')),
                                'reason': a.get('reason'),
                                'score': a.get('score')})
        if item:
            item['aliases'] = aliases
        return web.json_response({'item': item, 'aliases': aliases})

    async def alias_lookup(self, request):
        platform, pid = request.match_info['platform'], request.match_info['pid']
        rec = self.ledger.get_alias(platform, pid)
        if not rec:
            return web.json_response({'error': 'not in ledger'}, status=404)
        return web.json_response({'alias': rec})

    async def alias_user(self, request):
        platform, uid = request.match_info['platform'], request.match_info['uid']
        sid = self.registry.sid_of(platform, uid)
        if not sid:
            return web.json_response({'error': 'account not linked'}, status=404)
        rec = self.registry.source(sid)
        return web.json_response({'sid': rec['sid'], 'name': rec['name'],
                                  'accounts': rec['accounts']})

    # ---------- 订阅 feed（canonical 单流，尾注 D10） ----------

    async def feed(self, request):
        kind = 'atom' if request.path.startswith('/atom/') else 'rss'
        try:
            sid = int(request.match_info['sid'])
        except ValueError:
            return web.Response(status=400, text='bad sid')
        rec = self.registry.source(sid)
        if not rec:
            return web.Response(status=404, text='source not subscribed')
        limit = self._int(request, 'limit', 20, 1, 100)
        recs = self.ledger.timeline_for_sid(sid, limit=limit)
        items = [it for it in
                 [await self._render_item(r) for r in recs] if it]
        body = self._build_feed(kind, rec, items)
        ctype = 'application/atom+xml' if kind == 'atom' else 'application/rss+xml'
        return web.Response(text=body, content_type=ctype, charset='utf-8')

    def _build_feed(self, kind: str, src: dict, items: List[dict]) -> str:
        base = self.external_base_url
        esc = _html.escape
        channel_link = f'{base}/data/sd/source/{src["sid"]}'
        title = f'{src["name"]}（SwiftDedup 信源{pad_sid(src["sid"])}）'
        head = ('<?xml version="1.0" encoding="utf-8"?>\n'
                + ('<feed xmlns="http://www.w3.org/2005/Atom">\n' if kind == 'atom'
                   else '<rss version="2.0">\n<channel>\n'))
        if kind == 'atom':
            head += (f'  <title>{esc(title)}</title>\n'
                     f'  <id>{esc(channel_link)}</id>\n'
                     f'  <link href="{esc(channel_link)}"/>\n'
                     '  <updated>' + (items[0]['published'] if items else
                                      datetime.now(timezone.utc).isoformat()) + '</updated>\n')
        else:
            head += (f'  <title>{esc(title)}</title>\n'
                     f'  <link>{esc(channel_link)}</link>\n'
                     '  <description>SwiftDedup 信源聚合推流；先到为主，重复静默</description>\n')
        parts = [head]
        for it in items:
            guid = it['aid'] or f"{it['platform']}:{it['pid']}"
            html_body = _escape_html_text(it['text'], it['images'])
            link = esc(it['link'])
            if kind == 'atom':
                parts.append(
                    '  <entry>\n'
                    f'    <title>{esc(it["text"].split(chr(10))[0][:40])}</title>\n'
                    f'    <id>urn:swift:post:{esc(guid)}</id>\n'
                    f'    <link href="{link}"/>\n'
                    f'    <updated>{it["published"]}</updated>\n'
                    f'    <published>{it["published"]}</published>\n'
                    f'    <content type="html">{html_body.replace("]]>", "]]&gt;")}</content>\n'
                    '  </entry>\n')
            else:
                parts.append(
                    '    <item>\n'
                    f'      <title>{esc(it["text"].split(chr(10))[0][:40])}</title>\n'
                    f'      <link>{link}</link>\n'
                    f'      <guid isPermaLink="false">{esc(guid)}</guid>\n'
                    f'      <pubDate>{it["pubDate"]}</pubDate>\n'
                    f'      <description><![CDATA[{html_body}]]></description>\n'
                    '    </item>\n')
        tail = ('</feed>' if kind == 'atom' else '</channel>\n</rss>')
        parts.append(tail)
        return ''.join(parts)

    # ---------- api：管理 ----------

    async def api_status(self, request):
        try:
            self.registry.check_reload()
        except Exception:
            pass
        state = self.ledger.seed_state()
        return web.json_response({
            'engine': self.engine.status(),
            'seed': {'done': bool(state.get('done')), 'counts': state.get('counts') or {}},
            'unclaimed': self.registry.unclaimed(),
            'sources': len(self.registry.all_sources()),
        })

    async def api_mode(self, request):
        body = await request.json()
        mode = str(body.get('mode') or '')
        if not self.engine.set_mode(mode):
            return web.json_response({'error': 'mode must be shadow|enforce'}, status=400)
        return web.json_response({'engine': self.engine.status()})

    async def api_seed(self, request):
        body = await request.json() if request.can_read_body else {}
        force = bool(body.get('force'))
        seeder = self.seeder
        if not seeder:
            return web.json_response({'error': 'seeder not mounted'}, status=500)
        state = await seeder.run(force=force)
        return web.json_response({'seed': {'done': bool(state.get('done')),
                                           'counts': state.get('counts') or {}}})

    async def api_log(self, request):
        limit = self._int(request, 'limit', 200, 1, 2000)
        return web.json_response({'events': self.ledger.read_log(limit)})

    async def api_release(self, request):
        body = await request.json()
        platform = str(body.get('platform') or '')
        pid = str(body.get('post_id') or body.get('pid') or '')
        if not _plat_has(platform) or not pid:
            return web.json_response({'error': 'platform/post_id required'}, status=400)
        rec = self.engine.release(platform, pid)
        if not rec:
            return web.json_response({'error': 'alias not found'}, status=404)
        # 重复帖放行时补图片下载（如平台实现了 ensure_images）
        try:
            svc = self.services.get(platform)
            if svc and hasattr(svc, 'ensure_images'):
                await svc.ensure_images(rec.get('uid'), pid)
        except Exception as e:
            logger.warning(f"[sd] 放行补图失败 {pid}: {e}")
        return web.json_response({'released': True, 'alias': rec})

    async def api_source_create(self, request):
        body = await request.json()
        try:
            sid = self.registry.create_source(
                sid=body.get('id') or None, name=str(body.get('name') or ''))
        except ValueError as e:
            return web.json_response({'error': str(e)}, status=409)
        return web.json_response({'sid': sid,
                                  'sid3': pad_sid(sid),
                                  'source': self.registry.source(sid)})

    async def api_source_patch(self, request):
        sid = request.match_info['sid']
        body = await request.json()
        rec = self.registry.source(int(sid))
        if not rec:
            return web.json_response({'error': 'source not found'}, status=404)
        if 'name' in body:
            self.registry.rename_source(int(sid), str(body.get('name') or rec['name']))
        accounts = body.get('accounts') or {}
        reissued = {}
        for p, uids in accounts.items():
            if not _plat_has(p):
                continue
            for act in uids:
                old_sid = self.registry.sid_of(p, str(act))
                self.registry.upsert_link(p, act, int(sid))
                # 重新关联到不同信源 → 该账号动态按新信源重发聚合id（D19）
                if old_sid is not None and int(old_sid) != int(sid) and self.engine:
                    reissued[f'{p}:{act}'] = self.engine.reissue_for_account(
                        p, str(act), int(sid))
        return web.json_response({'source': self.registry.source(int(sid)),
                                  'reissued': reissued})

    async def api_link(self, request):
        """抓取名单账号 ↔ 信源号绑定/解绑（信源管理页 watchlist 卡）"""
        body = await request.json()
        platform = str(body.get('platform') or '')
        uid = str(body.get('uid') or '').strip()
        try:
            sid = int(body.get('sid') or 0)
        except (TypeError, ValueError):
            sid = 0
        if not _plat_has(platform) or not uid:
            return web.json_response({'error': 'platform/uid required'}, status=400)
        old_sid = self.registry.sid_of(platform, uid)
        reissued = {}
        if sid > 0 and self.registry.source(sid) is None:
            return web.json_response({'error': 'source not found'}, status=404)
        if sid > 0:
            self.registry.upsert_link(platform, uid, sid)
            # 重新关联到不同信源 → 该账号动态按新信源重发聚合id（D19）
            if old_sid is not None and int(old_sid) != sid and self.engine:
                reissued[f'{platform}:{uid}'] = self.engine.reissue_for_account(
                    platform, uid, sid)
        elif old_sid is not None:
            self.registry.unlink(platform, uid, int(old_sid))
        return web.json_response({'ok': True, 'sid': sid or None,
                                  'reissued': reissued,
                                  'unclaimed': self.registry.unclaimed()})

    async def api_source_delete(self, request):
        """删除信源：其下已关联账号回到待认领；号不自动回收"""
        try:
            sid = int(request.match_info['sid'])
        except ValueError:
            return web.json_response({'error': 'bad sid'}, status=400)
        if not self.registry.delete_source(sid):
            return web.json_response({'error': 'source not found'}, status=404)
        return web.json_response({'deleted': sid,
                                  'unclaimed': self.registry.unclaimed()})

    async def api_source_unlink(self, request):
        sid = request.match_info['sid']
        platform = request.match_info['platform']
        uid = request.match_info['uid']
        if not self.registry.unlink(platform, uid, int(sid)):
            return web.json_response({'error': 'link not found'}, status=404)
        return web.json_response({'unlinked': True, 'unclaimed': self.registry.unclaimed()})

    # ---------- 抓取名单（平台 watchlist） ----------

    async def _watchlist_of(self, platform: str) -> List[dict]:
        """某平台的本地关注名单（uid + 昵称）"""
        svc = self.services.get(platform)
        storage = getattr(svc, 'storage', None) if svc else None
        if storage is None:
            return []
        try:
            users = await storage.get_watchlist()
        except Exception:
            return []
        out = []
        for uid in sorted(users, key=lambda x: str(x)):
            try:
                name = storage.get_nickname(str(uid)) or ''
            except Exception:
                name = ''
            sid = self.registry.sid_of(platform, str(uid))
            out.append({'uid': str(uid), 'name': name,
                        'sid': int(sid) if sid else 0,
                        'sid3': pad_sid(sid) if sid else ''})
        return out

    # ---------- 昵称管理（跨平台全集 + 重名对照） ----------

    def _nick_file(self, platform: str) -> Path:
        cfg = getattr(self.services.get(platform), 'config', None)
        path = './data/{p}/watchlist.json'.format(p=platform)
        try:
            path = str(cfg.watchlist.get('file', path))
        except Exception:
            pass
        return Path(path)

    def _read_watch_json(self, platform: str) -> dict:
        f = self._nick_file(platform)
        try:
            raw = f.read_text(encoding='utf-8')
            w = json.loads(raw)
            return w
        except Exception as e:
            logger.warning(f'[昵称读取失败] {platform}: {e!r} raw前40={f.read_text(encoding="utf-8", errors="ignore")[:40]}')
            return {'users': [], 'nicknames': {}}

    async def api_nicknames(self, request):
        """昵称全集（含未订阅账号的缓存） + 跨平台同名对照"""
        plats = _plat_names()
        data = {}
        try:
            for plat2 in plats:
                nf = self._nick_file(plat2)
                logger.debug(f'[昵称] {plat2} path={nf.absolute()} exists={nf.exists()}')
        except Exception as e:
            logger.debug(f'[昵称] 解析失败: {e}')
        for plat in plats:
            w = self._read_watch_json(plat)
            data[plat] = {str(k): str(v) for k, v in (w.get('nicknames') or {}).items()}
        # 跨平台同名对照：按昵称聚合各平台的 uid
        cross = {}
        by_name: dict = {}
        for plat in plats:
            for uid, name in data[plat].items():
                by_name.setdefault(name, {}).setdefault(plat, []).append(uid)
        for name, plats_map in by_name.items():
            if len(plats_map) > 1:
                cross[name] = {'name': name, 'plats': plats_map}
        payload = {p: data[p] for p in plats}
        payload['cross'] = cross
        return web.json_response(payload)

    async def api_nickname_save(self, request):
        body = await request.json()
        platform = str(body.get('platform') or '')
        uid = str(body.get('uid') or '').strip()
        name = str(body.get('name') or '').strip()
        if not _plat_has(platform) or not uid:
            return web.json_response({'error': 'platform/uid required'}, status=400)
        svc = self.services.get(platform)
        storage = getattr(svc, 'storage', None)
        if storage is None:
            f = self._nick_file(platform)
            w = self._read_watch_json(platform)
            if name:
                (w.setdefault('nicknames', {}))[uid] = name
            else:
                (w.get('nicknames') or {}).pop(uid, None)
            f.write_text(json.dumps(w, ensure_ascii=False, indent=2), encoding='utf-8')
        else:
            try:
                await storage.set_nickname(uid, name)
            except Exception:
                pass
        return web.json_response({'saved': True, 'platform': platform, 'uid': uid})

    async def api_watchlist(self, request):
        payload = {}
        for p in _plat_names():
            payload[f'{p}'] = await self._watchlist_of(p)
        return web.json_response(payload)

    async def api_watchlist_add(self, request):
        body = await request.json()
        platform = str(body.get('platform') or '')
        uid = str(body.get('user_id') or body.get('uid') or '').strip()
        if not _plat_has(platform) or not uid:
            return web.json_response({'error': 'platform/user_id required'}, status=400)
        svc = self.services.get(platform)
        storage = getattr(svc, 'storage', None) if svc else None
        if storage is None:
            return web.json_response({'error': f'{platform} 平台未启用'}, status=400)
        try:
            added = await storage.add_to_watchlist(uid)
            if body.get('name'):
                await storage.set_nickname(uid, str(body['name']))
            # 平台浏览器侧也持有一份名单（长文补全等用），同步刷新
            bm = getattr(svc, 'bm', None)
            if bm is not None and hasattr(bm, 'watchlist'):
                bm.watchlist = await storage.get_watchlist()
            return web.json_response({'added': bool(added), 'uid': uid,
                                      'platform': platform})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def api_watchlist_del(self, request):
        platform = request.match_info['platform']
        uid = request.match_info['uid']
        svc = self.services.get(platform)
        storage = getattr(svc, 'storage', None) if svc else None
        if storage is None:
            return web.json_response({'error': f'{platform} 平台未启用'}, status=400)
        try:
            ok = await storage.remove_from_watchlist(uid)
            bm = getattr(svc, 'bm', None)
            if bm is not None and hasattr(bm, 'watchlist'):
                bm.watchlist = await storage.get_watchlist()
            return web.json_response({'removed': bool(ok), 'uid': uid,
                                      'platform': platform})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    # ---------- ui：面板 ----------

    def _render(self, html: str) -> str:
        """页面渲染时注入：平台引导(PLATFORMS)、导航平台链接、平台标签配色"""
        boot = _json.dumps(
            [[n, _plat_display(n)] for n in _plat_names()], ensure_ascii=False)
        nav = ''.join(f'<a href="/ui/{n}/">{_plat_display(n)}面板</a>'
                      for n in _plat_names())
        css = '\n'.join(_plat_attr(n, 'tag_css') or '' for n in _plat_names())
        return html.replace('__PLAT_BOOT__', boot) \
                   .replace('__NAV_PLAT__', nav).replace('__PLAT_CSS__', css)

    async def dashboard_page(self, request):
        return web.Response(text=self._render(_DASHBOARD_HTML),
                            content_type='text/html', charset='utf-8')

    async def api_stats_body(self):
        state = self.ledger.seed_state()
        return {
            'engine': self.engine.status(),
            'seed': {'done': bool(state.get('done')), 'counts': state.get('counts') or {}},
            'unclaimed': self.registry.unclaimed(),
            'sources': [{'sid': s['sid'], 'sid3': pad_sid(s['sid']), 'name': s['name'],
                         'accounts': s['accounts'],
                         'counters': self.ledger.counter_status(s['sid'])}
                        for s in self.registry.all_sources()],
            'recent_log': self.ledger.read_log(30),
        }

    async def events_page(self, request):
        return web.Response(text=self._render(_EVENTS_HTML),
                            content_type='text/html', charset='utf-8')

    async def dashboard_stats(self, request):
        try:
            self.registry.check_reload()
        except Exception:
            pass
        return web.json_response(await self.api_stats_body())

    async def dashboard_events(self, request):
        """SSE：增量推送判重日志行"""
        resp = web.StreamResponse(headers={'Content-Type': 'text/event-stream',
                                           'Cache-Control': 'no-cache'})
        await resp.prepare(request)
        self._sse_clients.append(resp)
        try:
            while not request.transport.is_closing():
                events = self.ledger.read_log(20)
                await resp.write(f"data: {_dump_json({'events': events})}\n\n".encode('utf-8'))
                await _sleep(3)
        except Exception:
            pass
        finally:
            if resp in self._sse_clients:
                self._sse_clients.remove(resp)
        return resp

    async def qrcode_image(self, request):
        """登录流程运行的实时落盘文件；只在面板点开时被拉取（spec §9.4）"""
        platform = request.match_info['platform']
        if platform not in PLATFORM_NAME or platform == 'sd':
            return web.json_response({'error': 'unknown platform'}, status=404)
        path = Path('./data') / platform / 'qrcode.png'
        if not path.exists():
            return web.Response(status=404, text='no pending qrcode (未在扫码流程中)')
        return web.Response(body=path.read_bytes(), content_type='image/png',
                            headers={'Cache-Control': 'no-store'})

    # ---------- utils ----------

    @staticmethod
    def _int(request, key, default, lo, hi):
        try:
            v = int(request.query.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    @staticmethod
    def _float(request, key, default):
        try:
            v = float(request.query.get(key))
        except (TypeError, ValueError):
            return default
        return v


# ---------- 杂项 ----------

def _escape_html_text(text: str, images: List[str]) -> str:
    esc = _html.escape
    body = esc(text).replace(chr(10), '<br/>')
    for img in images or []:
        body += f'<img src="{esc(img)}" loading="lazy"/>'
    return body


def _dump_json(obj) -> str:
    import json as _json
    return _json.dumps(obj, ensure_ascii=False)


async def _sleep(sec: float):
    import asyncio
    await asyncio.sleep(sec)


_EVENTS_HTML = '''
<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SwiftDedup · 信源面板</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1d2129;--border:#2a2f3a;--text:#d7dce4;--dim:#8b93a3;--accent:#4f8cff;--green:#3ad07a;--yellow:#e2b93b;--red:#ef5466;--cyan:#38c3c2}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:17px/1.55 "Cascadia Code","Microsoft YaHei UI","Segoe UI",monospace,sans-serif;padding:14px 16px 40px}
h1{font-size:26px;font-weight:600;letter-spacing:.5px;margin-bottom:2px}
h1 .tstamp{color:var(--dim);font-weight:400}
.row{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px;overflow:hidden;margin-bottom:12px}
.panel h2{font-size:16px;color:var(--dim);font-weight:600;letter-spacing:1px;margin-bottom:10px;text-transform:uppercase}
table{width:100%;border-collapse:collapse;font-size:16px}
td,th{padding:5px 8px;white-space:nowrap}
#src td{white-space:normal}
th{color:var(--dim);font-weight:500;text-align:left;border-bottom:1px solid var(--border);font-size:15px}
td{border-bottom:1px solid #232833}
tr:last-child td{border-bottom:none}
tr:hover td{background:#20242e}
.num{font-variant-numeric:tabular-nums}
.fresh{color:var(--green)} .warn{color:var(--yellow)} .dim{color:var(--dim)} .stale{color:var(--red)}
.small{font-size:14px;color:var(--dim);white-space:normal}
.live-log{background:#0b0d11;border:1px solid var(--border);border-radius:8px;height:272px;overflow-y:auto;padding:8px 10px;font-size:16px;scrollbar-width:thin}
.maxbox{max-height:272px;overflow-y:auto;scrollbar-width:thin}
.ll{white-space:pre-wrap;word-break:break-all;padding:1px 0}
.ll .t{color:#5d6675}
.ll .k{color:var(--cyan);font-weight:600}
.lv-INFO{color:var(--green)} .lv-DEBUG{color:#6a7383} .lv-WARNING{color:var(--yellow)} .lv-ERROR{color:var(--red)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:17px}
.kv dt{color:var(--dim)} .kv dd{overflow:hidden;text-overflow:ellipsis}
.event{display:flex;gap:10px;padding:6px 2px;border-bottom:1px dashed #232833;font-size:17px}
.event:last-child{border:none}
.e-time{color:var(--dim);flex:none;width:64px}
.e-dot{flex:none;width:9px;height:9px;border-radius:50%;margin-top:6px}
.pill{font-size:14px;padding:1px 8px;border-radius:10px;border:1px solid var(--border);color:var(--dim)}
.pill.badge{background:#24343c;color:var(--cyan);border-color:#2e5d63}
.ctl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
.btn{background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:4px 12px;font-size:15px;cursor:pointer}
.btn:hover{border-color:var(--accent)}
.btn.on{background:#2b4d8f;border-color:var(--accent);color:#e8f0ff;font-weight:600}
input{background:#0b0d11;border:1px solid var(--border);color:var(--text);border-radius:6px;padding:4px 10px;font-size:15px;width:200px}
.acct{margin-right:10px;color:var(--cyan)}
.mute{color:var(--dim);font-size:14px}
.topnav{display:flex;gap:22px;align-items:center;flex-wrap:wrap;background:#1f2833;border-bottom:2px solid #2a2f3a;padding:12px 20px;margin:-14px -16px 14px}
.topnav b{color:#fff;font-size:20px;margin-right:10px;font-weight:600}
.topnav a{color:#8ab;font-size:20px;text-decoration:none;opacity:.9}
.topnav a:hover{opacity:1;text-decoration:underline}
@media (max-width:760px){body{padding:10px}.topnav{gap:10px;padding:10px 12px}.topnav a{font-size:16px}.topnav b{font-size:16px}.live-log{height:260px}}
</style></head><body>
<header class="topnav"><b>信源面板</b>
 <a href="/ui/">门户</a><a href="/ui/login/">登录扫码</a><a href="/ui/sd/">信源管理</a>
 __NAV_PLAT__
 <a href="/ui/opener/">操作账号</a><a href="/ui/config/">配置</a></header>

<h1>信源面板（判重 / 聚合） <span class="tstamp" id="tstamp">--:--:--</span></h1>
<div style="margin-bottom:12px" id="hdr"></div>

<div class="row">
  <div class="panel">
    <h2>总览</h2>
    <dl class="kv" id="sys"></dl>
  </div>
  <div class="panel">
    <h2>重要事件</h2>
    <div id="events" class="maxbox"></div>
  </div>
</div>

<div class="panel">
  <h2>聚合账号（信源 / 聚合号发放）</h2>
  <div class="maxbox"><table id="src"><thead><tr>
    <th>信源号</th><th>名称</th><th>账号</th><th>近3日聚合号</th>
  </tr></thead><tbody></tbody></table>
  <div class="mute" style="margin-top:6px">账号列 = 昵称 + UID；聚合号列 = 当日跨平台聚合帖编号（发放数）。</div></div>
</div>

<div class="panel">
  <h2>实时日志（聚合判重 SSE）</h2>
  <div class="ctl">
    <button class="btn on" id="pause">暂停滚动</button>
    <button class="btn" data-lv="DEBUG">DEBUG</button>
    <button class="btn on" data-lv="INFO">INFO</button>
    <button class="btn on" data-lv="WARNING">WARN</button>
    <button class="btn on" data-lv="ERROR">ERROR</button>
    <input id="q" placeholder="搜索关键字…">
    <span class="pill" id="preset">显示 0 / 总 0</span>
  </div>
  <div class="live-log" id="log"></div>
  <div class="mute" style="margin-top:6px">来源：/ui/sd/events（dedup ledger 实时流，默认展示 INFO 及以上）；各平台自身日志见各自面板。</div>
</div>

<script>
const PLATFORMS=__PLAT_BOOT__;
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
const fmtCN = ts => { if(ts==null) return "—"; const d = new Date(ts*1000);
  return `${d.getMonth()+1}月${d.getDate()}日 ${d.toTimeString().slice(0,5)}`; };

const KIND_CN = {dup:'重复', duplicate:'重复', canonical:'主帖', exact:'精确命中',
  near_miss:'近失', unclaimed:'未关联', released:'放行', mode:'模式',
  seed_done:'播种完成', fingerprint:'指纹', known:'已见过', empty_text:'无文字',
  skipped_lottery:'抽奖跳过'};
const REASON_CN = {exact:'精确', fuzzy:'模糊', lottery:'抽奖', window:'时间窗'};
const KIND_LV = {canonical:'INFO', dup:'INFO', exact:'INFO', near_miss:'WARNING',
  unclaimed:'INFO', released:'INFO', mode:'INFO', seed_done:'INFO', fingerprint:'DEBUG'};
const MODE_CN2 = {shadow:'影子(观察)', enforce:'执行(强制)'};
const DOT = {mode:'var(--cyan)', seed_done:'var(--green)', released:'var(--green)',
  unclaimed:'var(--red)', near_miss:'var(--yellow)'};

// —— 级别过滤（默认 INFO 及以上） ——
const LV_ORDER = ["DEBUG","INFO","WARNING","ERROR"];
let lvOn = {DEBUG:false, INFO:true, WARNING:true, ERROR:true};
document.querySelectorAll('[data-lv]').forEach(b => b.onclick = () => {
  lvOn[b.dataset.lv] = !lvOn[b.dataset.lv];
  b.classList.toggle('on', lvOn[b.dataset.lv]);
  render(false);
});
$('#pause').onclick = e => { paused = !paused; e.target.classList.toggle('on', !paused); };
$('#q').oninput = e => { q = e.target.value; render(false); };

// —— SSE 日志缓冲（3s 轮替推送最近 20 条，按 id 去重合并） ——
let buf = [], total = 0, q = "", paused = false;
const seen = new Set();
const evId = ev => [ev.time, ev.kind || ev.lv, ev.platform, ev.pid, ev.uid, ev.sid,
  ev.dup_of ? ev.dup_of.pid : '', ev.msg || ''].join('|');
function pushEvent(ev){
  if (ev.ev) ev = ev.ev;
  const id = evId(ev);
  if (seen.has(id)) return;
  seen.add(id);
  buf.push(ev); total++;
  if (buf.length > 600){ const drop = buf.shift(); seen.delete(evId(drop)); }
  if (!paused) render();
}
function line(ev){
  const lv = ev.lv || KIND_LV[ev.kind] || 'INFO';
  const t = ev.time ? new Date(ev.time*1000).toTimeString().slice(0,8) : (ev.t || '');
  if (ev.msg !== undefined && ev.kind === undefined)
    return `<div class="ll"><span class="t">${esc(t)}</span> | <span class="lv-${esc(lv)}">${(lv+"      ").slice(0,7)}</span> ${esc(ev.msg)}</div>`;
  const parts = [];
  if (ev.platform) parts.push(esc(ev.platform));
  if (ev.pid) parts.push('pid=' + esc(ev.pid));
  if (ev.uid != null) parts.push('user=' + esc(nickOf(ev.platform, ev.uid) || ev.uid));
  if (ev.sid != null) parts.push('sid=' + esc(ev.sid));
  if (ev.score != null) parts.push(Math.round(ev.score*100) + '%');
  if (ev.mode) parts.push('模式 ' + esc(MODE_CN2[ev.mode] || ev.mode));
  if (ev.reason) parts.push(esc(REASON_CN[ev.reason] || ev.reason));
  if (ev.counts) parts.push(Object.entries(ev.counts).map(([k,v])=>esc(k)+'×'+v).join(' '));
  if (ev.dup_of) parts.push('→ ' + esc(ev.dup_of.platform || '') + ':' + esc(ev.dup_of.pid || ''));
  const kc = `<span class="k">${esc(KIND_CN[ev.kind] || ev.kind || '·')}</span>`;
  return `<div class="ll"><span class="t">${esc(t)}</span> | <span class="lv-${esc(lv)}">${(lv+"      ").slice(0,7)}</span> ${kc} ${esc(parts.join('  '))}</div>`;
}
function render(stick = true){
  const k = (q || "").toLowerCase();
  const rows = buf.filter(ev => {
    const lv = ev.lv || KIND_LV[ev.kind] || 'INFO';
    if (!lvOn[lv]) return false;
    if (!k) return true;
    const txt = (ev.msg || '') + ' ' + (ev.pid || '') + ' ' + (ev.platform || '') + ' '
    + (ev.text || '') + ' ' + (KIND_CN[ev.kind] || '');
    return txt.toLowerCase().includes(k);
  }).slice(-300);
  $('#log').innerHTML = rows.slice().reverse().map(line).join('');
  const el = $('#log');
  if (stick) el.scrollTop = 0;
  $('#preset').textContent = `显示 ${el.childElementCount} / 总 ${total}`;
}
const es = new EventSource('/ui/sd/events');
es.onopen  = () => $('#preset').classList.add('badge');
es.onerror = () => $('#preset').classList.remove('badge');
es.onmessage = e => {
  try{ (JSON.parse(e.data).events || []).forEach(pushEvent); }catch(_){}
};

// —— 轮询 /ui/sd/stats：总览 + 重要事件 + 聚合账号 ——
let pollMs = 3000, pollTimer = null, nick = {};
const nickOf = (p, u) => ((nick[p] || {})[String(u)]) || '';
function pollBtns(){
  return [1000,3000,5000,10000,0].map(ms =>
    '<span class="btn ' + (pollMs === ms ? 'on' : '') + '" data-ms="' + ms + '">' +
    (ms ? (ms/1000) + 's' : '暂停') + '</span>').join('');
}
function setPoll(ms){
  pollMs = ms;
  if (pollTimer){ clearInterval(pollTimer); pollTimer = null; }
  if (ms) pollTimer = setInterval(poll, ms);
  $('#hdr').innerHTML = '<span class="pill">刷新</span> ' + pollBtns();
  document.querySelectorAll('#hdr [data-ms]').forEach(x =>
    x.classList.toggle('on', Number(x.dataset.ms) === pollMs));
}
$('#hdr').addEventListener('click', e => {
  const b = e.target.closest('[data-ms]');
  if (b) setPoll(Number(b.dataset.ms));
});
function setHTML(el, html){ if (el.__h !== html){ el.__h = html; el.innerHTML = html; } }

async function poll(){
  try{
    const d = await (await fetch('/ui/sd/stats')).json();
    const eng = d.engine || {};
    $('#tstamp').textContent = new Date().toTimeString().slice(0,8);
    // —— 总览 ——
    const today = new Date();
    const ymd = today.getFullYear() + String(today.getMonth()+1).padStart(2,'0') +
                String(today.getDate()).padStart(2,'0');
    const issued = (d.sources || []).reduce((acc, s) => {
      const c = s.counters || {};
      return acc + Object.entries(c).reduce((a, [k, v]) => a + (k === ymd ? v : 0), 0);
    }, 0);
    const linked = (d.sources || []).reduce((acc, s) => {
      const a = s.accounts || {};
      return acc + PLATFORMS.reduce((m, [p]) => m + ((a[p] || []).length || 0), 0);
    }, 0);
    const un = d.unclaimed || {};
    const seed = d.seed || {};
    const seedTxt = seed.done ? '<span class="fresh">完成</span>' :
      PLATFORMS.map(([p, label]) => esc(label) + ' ' + ((seed.counts||{})[p] || 0)).join(' / ');
    setHTML($('#sys'),
      `<dt>判重模式</dt><dd class="${eng.mode === 'enforce' ? 'fresh' : 'warn'}">${esc(MODE_CN2[eng.mode] || eng.mode || '—')}${eng.enabled ? '' : ' (未启用)'}</dd>`+
      `<dt>模糊阈值 / 时间窗</dt><dd>${eng.fuzzy_threshold == null ? '—' : eng.fuzzy_threshold} / ${eng.window_hours == null ? '—' : eng.window_hours}h</dd>`+
      `<dt>播种</dt><dd>${seedTxt}</dd>`+
      `<dt>信源 / 已关联账号</dt><dd>${(d.sources || []).length} / ${linked}</dd>`+
      `<dt>待认领（捕捉未关联）</dt><dd class="${PLATFORMS.some(([p]) => ((un[p]||[]).length) > 0) ? 'warn' : 'dim'}">${PLATFORMS.map(([p, label]) => esc(label) + ' ' + ((un[p]||[]).length)).join(' · ')}</dd>`+
      `<dt>今日聚合号发放</dt><dd class="${issued ? 'fresh' : 'dim'}">${issued}</dd>`);
    // —— 重要事件 ——
    const imp = (d.recent_log || []).filter(ev => {
      const lv = ev.lv || KIND_LV[ev.kind] || 'INFO';
      return ['mode','seed_done','unclaimed','released','near_miss'].includes(ev.kind) ||
             lv === 'WARNING' || lv === 'ERROR';
    }).slice(0, 12);
    setHTML($('#events'), imp.map(ev => {
      const t = ev.time ? new Date(ev.time*1000).toTimeString().slice(0,5) : '';
      const txt = ev.msg !== undefined && ev.kind === undefined
        ? esc(ev.msg)
        : esc((KIND_CN[ev.kind] || ev.kind || '·') +
            (ev.platform ? ' · ' + ev.platform : '') +
            (ev.pid ? ' · ' + ev.pid : '') +
            (ev.sid != null ? ' · sid=' + ev.sid : '') +
            (ev.mode ? ' → ' + (MODE_CN2[ev.mode] || ev.mode) : '') +
            (ev.counts ? ' · ' + Object.entries(ev.counts).map(([k,v])=>k+'×'+v).join(' ') : ''));
      return `<div class="event"><span class="e-time">${esc(t)}</span>` +
        `<span class="e-dot" style="background:${DOT[ev.kind] || 'var(--accent)'}"></span>` +
        `<span>${txt}</span></div>`;
    }).join(''));
    // —— 聚合账号表 ——
    const nickName = (plat, uid) => ((nick[plat] || {})[String(uid)]) || '';
    setHTML($('#src tbody'), (d.sources || []).map(s => {
      const a = s.accounts || {};
      const acc = PLATFORMS.map(([p]) => {
        const us = (a[p] || []);
        return us.length
          ? us.map(uid => `<span class="acct">${esc(nickName(p, uid))} <span class="dim">${esc(uid)}</span></span>`).join('')
          : '<span class="dim">—</span>';
      }).join(' ');
      const cnt = Object.entries(s.counters || {}).sort((x, y) => y[0].localeCompare(x[0]))
        .slice(0, 3).map(([k, v]) =>
          `${esc(String(k).slice(6,8).replace(/^0/, ''))}日 ${v}`).join(' · ') || '—';
      return `<tr><td class="num">${esc(s.sid3 || s.sid)}</td><td>${esc(s.name || '')}</td>` +
             `<td>${acc}</td><td class="num">${cnt}</td></tr>`;
    }).join('') || '<tr><td colspan="4" class="dim">尚无信源</td></tr>');
  }catch(e){
    $('#tstamp').textContent = '⚠️ 拉取失败';
  }
}
fetch('/api/sd/nicknames').then(r => r.json()).then(d => { nick = d; poll(); }).catch(poll);
setPoll(3000);
</script>
</body></html>
'''

_DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SwiftDedup · 信源管理</title>
<style>
 body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#f5f6f8;color:#1e2430;font-size:20px}
 input,select,button,textarea{font-size:20px}
 header{background:#1f2833;color:#fff;padding:12px 20px;display:flex;gap:20px;align-items:center}
 header a{color:#8ab;color:inherit;text-decoration:none;opacity:.85}
 header a:hover{opacity:1}
 main{max-width:1100px;margin:20px auto;padding:0 16px}
 .card{background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:16px;margin-bottom:16px}
 h2{margin:0 0 10px;font-size:20px}
 table{width:100%;border-collapse:collapse;font-size:17px}
 td,th{padding:6px 8px;border-bottom:1px solid #eef0f3;text-align:left;vertical-align:top}
 .tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:16px;margin-right:4px}
 __PLAT_CSS__
 .tag.canonical{background:#e6ffe6;color:#282}.tag.exact{background:#fff0e0;color:#a52}
 .tag.near_miss{background:#f0f0f0;color:#888}
 .pill{display:inline-block;padding:2px 10px;border-radius:12px;background:#eef;color:#335;font-size:16px}
 input{padding:4px 8px;border:1px solid #ccd;border-radius:6px;margin-right:4px}
 button{padding:5px 12px;border:0;border-radius:6px;background:#356;color:#fff;cursor:pointer}
 button.warn{background:#a44}.btn-min{background:#889}
 .mute{color:#889;font-size:16px}
 .log td{font-family:Consolas,monospace;font-size:16px}
 .wl-row{display:flex;gap:8px;align-items:center;padding:4px 6px;border-bottom:1px solid #eef0f3;flex-wrap:wrap}
 .wl-u{font-family:Consolas,monospace;font-size:18px;min-width:150px}
 .wl-n{font-size:19px}
 .nk-wrap{display:grid;grid-template-columns:1fr 1fr;gap:14px}
 .nk-col h3{margin:0 0 8px;font-size:17px}
 .nk-row{display:flex;gap:8px;align-items:center;padding:4px 6px;border-bottom:1px solid #eef0f3;flex-wrap:wrap}
 .nk-row.cross{background:#e8f4ff}
 .nk-u{font-family:Consolas,monospace;font-size:15px;color:#667;min-width:136px}
 .nk-n{font-size:16px}
 @media (max-width:900px){.nk-wrap{grid-template-columns:1fr}}
</style></head><body>
<header><b>信源管理</b><a href="/ui/">门户</a><a href="/ui/login/">登录扫码</a><a href="/ui/sd/">信源管理</a><a href="/ui/events/">信源面板</a>__NAV_PLAT__<a href="/ui/opener/">操作账号</a><a href="/ui/config/">配置</a></header>
 
<main>
 <div class="card"><h2>信源管理</h2><div id="sources"></div>
  <div class="mute">新信源：号(可选) + 名称 → 新建；关联/解除在各行内进行；号不回收。</div>
  <form id="new-src" style="margin-top:8px">
    <input id="ns-id" placeholder="信源号(留空自动)"><input id="ns-name" placeholder="信源名">
    <button type="submit">新建信源</button></form>
  <div class="mute" style="margin-top:6px">信源=判重池；下面的“抓取名单”决定这些账号是否真的被监控。</div></div>
 <div class="card"><h2>抓取名单（平台 watchlist）</h2><div id="watchlist"></div>
  <form id="add-wl" style="margin-top:8px">
    <select id="wl-plat"></select>
    <input id="wl-uid" placeholder="账号 uid（数字）">
    <input id="wl-name" placeholder="昵称（可选）">
    <select id="wl-link"><option value="0">未关联</option></select>
    <button type="submit">加入抓取</button></form>
  <div class="mute">只有名单内的账号才会被监控/落盘；名单可在 sources.yaml 之外随时增删。
   部分平台还需该账号在浏览器的“关注”列表中。</div></div>
 <div class="card"><h2>待认领账号</h2><div id="unclaimed"></div></div>
 <div class="card"><h2>昵称管理（跨平台）</h2><div id="nicks"></div>
  <button id="nicks-refresh" class="btn-min">刷新昵称</button>
  <div class="mute" style="margin-top:6px">同一昵称同时出现在两个平台时标注「同名跨平台」；改名写入对应平台 watchlist.json 的 nicknames（随时热载）。改名留空=删除缓存。</div></div>
 <div class="card"><h2>判重日志（近失段一并记录）</h2>
  <div style="max-height:320px;overflow-y:auto;border:1px solid #dfe3ea;border-radius:8px">
  <table class="log" id="log"><thead><tr><th>时间</th><th>决定</th><th>平台 · 账号</th><th>动态id</th><th>详情</th></tr></thead>
  <tbody></tbody></table></div>
  <button id="more-log" class="btn-min">加载更多</button>
  <button id="toggle-mode" class="warn">模式切换：影子(观察) ⇄ 执行(强制)</button></div>
</main>
<script>
const PLATFORMS=__PLAT_BOOT__;
const PLAT_CN=Object.fromEntries(PLATFORMS);
const PLAT_OPTS=PLATFORMS.map(([v,n])=>'<option value="'+v+'">'+n+'</option>').join('');
if(document.getElementById('wl-plat')) document.getElementById('wl-plat').innerHTML = PLAT_OPTS;
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
const KIND_CN_EV = {duplicate:'重复', canonical:'主帖', exact:'精确命中', near_miss:'近失',
  unclaimed:'未关联', released:'放行', mode:'模式', seed_done:'播种完成',
  fingerprint:'指纹', skipped_lottery:'抽奖跳过', empty_text:'无文字', known:'已见过'};
const MOODE_UNUSED = 0;
function platOf(p){ return PLAT_CN[p] || p; }
function cntFmt(c){
  const ks = Object.keys(c).sort((a,b)=>b.localeCompare(a)).slice(0,3);
  return ks.map(k=>k.slice(4,6).replace(/^0/,'') + '月' + k.slice(6,8).replace(/^0/,'') + '日 ' + c[k]).join(' · ') || '—';
}
function modeOf(m){ return m; }
let logOffset = 0;
let lastMode = 'shadow';
const NICKD = {};   // 平台 → {uid: 昵称}
const nickOf = (p, u) => ((NICKD[p] || {})[String(u)]) || '';
async function api(p, opt){const r=await fetch(p,opt);return r.json();}
async function refresh(){
  const s = await api('/ui/sd/stats');
  lastMode = s.engine.mode;
  SIDS = (s.sources || []).map(x =>
    ({sid: x.sid, sid3: x.sid3, name: x.name}));  SRCNAMES = {};
  SIDS.forEach(x => SRCNAMES[x.sid] = x.name);
  loadWatchlist();
  document.getElementById('sources').innerHTML = s.sources.map(x=>{
    const accts = Object.entries(x.accounts||{}).map(([p,us])=>
      `<span class="tag ${p}">${platOf(p)}</span>`+us.map(u=>
        `<span class="tag ${p}">${esc(nickOf(p,u) || u)}</span><button class="btn-min" onclick="unlink('${x.sid}','${p}','${u}',this)">×</button>`).join('')
    ).join(' ');
    return `<div style="margin:6px 0"><b>${x.sid3||x.sid}</b> ${x.name}
      <span class="mute">近3日发放：${cntFmt(x.counters||{})}</span><br>${accts}
      <button class="btn-min" onclick="srcRename(${x.sid})">改名</button>
      <button class="btn-min warn" onclick="srcDel(${x.sid},this)">删除</button>
      <form style="display:inline;margin-left:6px" onsubmit="return linkIn(${x.sid})">
      <select id="lkg-${x.sid}">${PLAT_OPTS}</select>
      <input id="lk-${x.sid}" placeholder="账号 UID" style="width:170px">
      <button class="btn-min">关联</button></form></div>`}).join('')
    || '<div class="mute">尚未定义任何信源 — 到 sources.yaml 手动填写或下方新建</div>';
  document.getElementById('unclaimed').innerHTML =
    Object.entries(s.unclaimed||{}).map(([p,us])=> us.map(u=>
      `<span class="tag ${p}">${platOf(p)}：${esc(nickOf(p, u.uid||u) || u.uid||u)}<span class="dim">${esc(u.uid||u)}</span></span>`).join(' ')).join('<br>')
    || '<span class="mute">无</span>';
}
let SIDS = [];
function sidOpts(cur){
  const c = Number(cur) || 0;
  return '<option value="0"' + (c ? '' : ' selected') + '>未关联</option>'
    + SIDS.map(s => '<option value="' + s.sid + '"' + (c === s.sid ? ' selected' : '') + '>'
      + esc(s.sid3 || s.sid) + ' ' + esc(s.name || '') + '</option>').join('');
}
async function loadWatchlist(){
  let d = {};
  PLATFORMS.forEach(([p]) => d[p] = []);
  try { d = await api('/api/sd/watchlist'); } catch(e) {}
  document.getElementById('wl-link').innerHTML = sidOpts(0);
  const row = (p, x) => '<div class="wl-row">'
    + '<span class="tag ' + p + '">' + platOf(p) + '</span>'
    + '<span class="wl-u">' + esc(x.uid) + '</span>'
    + '<span class="wl-n" id="wn-' + p + '-' + x.uid + '">' + esc(x.name || '(无昵称)') + '</span>'
    + `<button class="btn-min" onclick="wlNick('${p}','${x.uid}')">改昵称</button>`
    + `<select onchange="linkAcc('${p}','${x.uid}', this.value)">${sidOpts(x.sid)}</select>`
    + `<button class="btn-min" onclick="wlDel('${p}','${x.uid}',this)">×</button></div>`
  const render = (p, arr) => (arr || []).length
    ? arr.map(x => row(p, x)).join('')
    : '<div class="mute">（空）</div>';
  document.getElementById('watchlist').innerHTML = PLATFORMS.map(([p, label], i) =>
    '<div' + (i ? ' style="margin-top:10px"' : '') + '><b>' + esc(label) + '</b></div>'
    + render(p, d[p])).join('');
}
async function wlNick(p, uid){
  const cur = document.getElementById('wn-' + p + '-' + uid).textContent;
  const name = prompt('新昵称（留空=清除）', cur === '(无昵称)' ? '' : cur);
  if (name === null) return;
  await api('/api/sd/nickname', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({platform:p, uid:uid, name:name.trim()})});
  loadWatchlist(); loadNicks();
}
async function linkAcc(p, uid, sidV){
  await api('/api/sd/link', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({platform:p, uid:uid, sid:Number(sidV) || 0})});
  loadWatchlist(); refresh();
}
async function wlDel(p, uid, btn){
  armDel(btn, async () => {
    await fetch(`/api/sd/watchlist/${p}/${uid}`, {method:'DELETE'});
    loadWatchlist();
  });
}
document.getElementById('add-wl').onsubmit = async function(e){
  e.preventDefault();
  const p = document.getElementById('wl-plat').value;
  const uid = document.getElementById('wl-uid').value.trim();
  const name = document.getElementById('wl-name').value.trim();
  const sid = Number(document.getElementById('wl-link').value) || 0;
  if(!uid) return;
  const r = await fetch('/api/sd/watchlist', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({platform:p, user_id:uid, name: name || undefined})});
  const d = await r.json().catch(()=>({}));
  if(d.error){ alert(d.error); return; }
  document.getElementById('wl-uid').value = '';
  document.getElementById('wl-name').value = '';
  if(sid) await linkAcc(p, uid, String(sid));
  loadWatchlist();
};
async function linkIn(sid){
  const p = document.getElementById('lkg-'+sid).value;
  const uid = document.getElementById('lk-'+sid).value.trim();
  if(!uid) return false;
  await api('/api/sd/link', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({platform:p, uid:uid, sid:sid})});
  document.getElementById('lk-'+sid).value = '';
  refresh(); loadWatchlist(); return false;
}
let SRCNAMES = {};
async function srcRename(sid){
  const name = prompt('信源新名称', SRCNAMES[sid] || '');
  if (name === null) return;
  await api('/api/sd/source/'+sid, {method:'PATCH', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name: name.trim()})});
  refresh();
}
function armDel(btn, fn){
  if (btn.dataset.armed === '1'){ fn(); return; }
  btn.dataset.armed = '1';
  btn.dataset.label = btn.textContent;
  btn.textContent = '确定删除？';
  btn.classList.add('warn');
  setTimeout(() => {
    if (btn.isConnected && btn.dataset.armed === '1'){
      btn.textContent = btn.dataset.label;
      btn.classList.remove('warn');
      delete btn.dataset.armed;
    }
  }, 3000);
}
async function srcDel(sid, btn){
  armDel(btn, async () => {
    await api('/api/sd/source/'+sid, {method:'DELETE'});
    refresh(); loadWatchlist();
  });
}
async function unlink(sid, p, u, btn){
  armDel(btn, async () => {
    await fetch(`/api/sd/source/${sid}/account/${p}/${u}`, {method:'DELETE'});
    refresh();
  });
}
document.getElementById('new-src').onsubmit = async function(e){
  e.preventDefault();
  const id = document.getElementById('ns-id').value.trim();
  const name = document.getElementById('ns-name').value.trim();
  await api('/api/sd/source',{method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id: id||null, name})});
  refresh();
};
document.getElementById('toggle-mode').onclick = async function(){
  const cur = lastMode.includes('enforce') ? 'shadow' : 'enforce';
  await api('/api/sd/mode',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:cur})});
  refresh();
};
async function loadNicks(){
  const d = await api('/api/sd/nicknames');
  Object.keys(d).filter(k => k !== 'cross').forEach(p => NICKD[p] = d[p]);
  const crossNames = new Set();
  Object.values(d.cross||{}).forEach(c=>crossNames.add(c.name));
  const mk = (plat, title) => {
    const entries = Object.entries(d[plat]||{}).sort((a,b)=>a[1].localeCompare(b[1],'zh'));
    return '<div><h3>'+title+'</h3>' + entries.map(([uid,name])=>{
      const cross = crossNames.has(name);
      return '<div class="nk-row'+(cross?' cross':'')+'">'
        + '<span class="nk-u">'+uid+'</span><span class="nk-n">'+esc(name)+'</span>'
        + (cross?'<span class="tag near_miss">同名跨平台</span>':'')
        + `<button class="btn-min" onclick="editNick('${plat}','${uid}',this)">改名</button></div>`;
    }).join('') + '</div>';
  };
  document.getElementById('nicks').innerHTML =
    '<div class="nk-wrap">'+PLATFORMS.map(([p, label])=>mk(p, label+'昵称')).join('')+'</div>';
}
function editNick(plat, uid, btn){
  const cur = btn.closest('.nk-row').querySelector('.nk-n').textContent;
  const name = prompt('新昵称（留空=删除该缓存）', cur);
  if (name === null) return;
  api('/api/sd/nickname',{method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({platform:plat, uid:uid, name:name.trim()})}).then(loadNicks);
}
document.getElementById('nicks-refresh').onclick = loadNicks;
loadNicks();

async function loadLog(){
  const d = await api('/api/sd/log/dedup?limit='+(60+logOffset));
  const rows = d.events.slice().reverse().map(ev=>{
    const who = ev.platform ? `${platOf(ev.platform)} <span class="dim">${esc(nickOf(ev.platform, ev.uid) || ev.uid || '')}</span>` : '';
    const what = ev.dup_of ? `→ ${platOf(ev.dup_of.platform)}:${ev.dup_of.pid} (${esc(ev.aid||ev.dup_of.aid||'?')})` : (ev.aid?`聚合号 ${esc(ev.aid)}`:'');
    const txt = ev.text ? `<div class="mute" style="white-space:normal">“${esc(ev.text)}”</div>` : '';
    return `<tr><td>${new Date((ev.time||0)*1000).toLocaleString()}</td><td>${KIND_CN_EV[ev.kind]||ev.kind||''}${ev.score?(' '+(ev.score*100|0)+'%'):''}</td>
      <td>${who}</td><td>${ev.pid||''}</td><td>${what}${txt}</td></tr>`}).join('');
  document.querySelector('#log tbody').innerHTML = rows;
  const logBox = document.querySelector('#log').parentElement;
  logBox.scrollTop = logBox.scrollHeight;
}
document.getElementById('more-log').onclick = function(){
  logOffset += 200; loadLog();
};
refresh(); loadLog();
setInterval(refresh, 5000);
</script></body></html>
'''
