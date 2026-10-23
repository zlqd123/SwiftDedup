# -*- coding: utf-8 -*-

"""判重引擎 — 泯池竞速的心脏（判重设计规范 §2 / §4 / §7）

唯一入库口：ingest(platform, uid, post)。平台插件零改动接入，只在落盘后回调。

判定顺序（每帖一次，幂等）：
  0. pid 已在别名账本 → 同帖重复抓取（编辑演化/快照差异不算重复，spec §2.4）：只刷新快照
  1. 未关联账号 → 指纹入挂起桶（acct:{platform}:{uid}），待关联后自动入池
  2. 同池内逐字哈希命中 → 逐字级重复（无时间限制；跨池永不比较，§2.3）
  3. 信源池内 模糊判重：同自然日(或|Δ|≤4h) 且 包含率≥0.60；0.40~0.60 记近失（影子盯防）
  4. 通过 → canonical：领聚合动态号（sid+日期+序号，重复帖不领号）

信源级 dedup 开关：每个信源可独立开/关（sources.yaml `dedup` 字段 / 面板信源卡）。
历史 shadow/enforce 双模式已移除；旧日志行中的 mode 字段仅作追溯。
"""

import time
from datetime import datetime, timedelta
from typing import Dict, Optional

from loguru import logger

from . import fingerprint as FP
from ..core.platform import DISPLAY_NAME as PLATFORM_NAME
from ..core.platform import attr as _plat_attr
from .registry import SourceRegistry
from .ledger import Ledger


def pad_sid(sid) -> str:
    """信源号段：基本 3 位补零（1-999）；未来同一族超过三位自动扩位（兼容）
    例：7 → '007'，123 → '123'，10001 → '10001'"""
    s = str(int(sid))
    return s.zfill(max(3, len(s)))


def pad_date(date_key: str) -> str:
    """日期段：去掉世纪的 2 位年份 → YYMMDD"""
    return date_key[2:] if date_key.startswith('20') and len(date_key) == 8 else date_key


def pad_serial(serial: int) -> str:
    """序号段：基本 3 位补零；单日单信源超过 999 自动扩位（兼容）"""
    s = str(int(serial))
    return s.zfill(max(3, len(s)))


def make_aggregate_id(sid, date_key: str, serial: int) -> str:
    """聚合动态号 = PPP + YYMMDD + NNN（基本 12 位定长；sid/序号超位自动扩位兼容）"""
    return f'{pad_sid(sid)}{pad_date(date_key)}{pad_serial(serial)}'


class DedupEngine:

    def __init__(self, config, registry: SourceRegistry, ledger: Ledger):
        self.config = config
        self.registry = registry
        self.ledger = ledger
        dd = (config.dedup if hasattr(config, 'dedup') else None) or self._default_cfg()
        self.cfg = dd
        self.threshold = float(dd.get('fuzzy_threshold', FP.FUZZY_THRESHOLD))
        self.window_hours = float(dd.get('window_hours', 1.0))
        self.near_low = float(dd.get('near_miss_low', FP.NEAR_MISS_LOW))
        self.enabled = bool(dd.get('enabled', True))
        # 未关联(unclaimed)批量计数：避免每帖一条刷屏 —— 启动批次 60s 合一，
        # 其后 15s 内的新增合一发送（仅计数新出现的 pid，已见过的不再发）
        self._unc_seen: set = set()
        self._unc_pending: dict = {}
        self._unc_first: float = 0.0
        self._unc_flushed_once: bool = False

    @staticmethod
    def _default_cfg():
        return {'fuzzy_threshold': FP.FUZZY_THRESHOLD,
                'window_hours': 1.0, 'near_miss_low': FP.NEAR_MISS_LOW,
                'enabled': True}

    # ---------- 状态 ----------

    def status(self) -> dict:
        return {'enabled': self.enabled,
                'fuzzy_threshold': self.threshold, 'window_hours': self.window_hours}


    # ---------- 发布时间 ----------

    @staticmethod
    def publish_ts(platform: str, post: dict) -> float:
        """发帖时间：解析逻辑在平台插件的 publish_ts hook"""
        fn = _plat_attr(platform, 'publish_ts')
        if fn is not None:
            return fn(post)
        return time.time()          # 未注册平台无法解析 → 按当前时间

    # ---------- 主入口 ----------

    async def ingest(self, platform: str, uid: str, post: dict) -> dict:
        """新帖落盘后回调。返回 {decision, sid, aid, ...} 供日志/调试，平台不依赖其业务语义

        注意：本回调发生在平台广告过滤与落盘**之后**，指纹拿到的是干净文本。
        聚合账本不存正文快照（D17）：只存指向平台原始缓存的引用字典。
        """
        if not self.enabled:
            return {'decision': 'disabled'}
        uid = str(uid)
        pid = extract_pid(platform, post)
        if not pid:
            return {'decision': 'no_pid'}
        ts = self.publish_ts(platform, post)

        # 0. 抽奖公示等"直接不推"内容：不进聚合、不判重（D22；平台缓存不受影响）
        lottery_fn = _plat_attr(platform, 'lottery_filtered')
        if lottery_fn is not None and lottery_fn(post):
            self._log(platform, pid, uid, ts, 'skipped_lottery')
            return {'decision': 'skipped_lottery'}

        # 0.5 同 pid 幂等：编辑演化/快照差异=同一帖，不重判（spec §2.4）
        known = self.ledger.get_alias(platform, pid)
        if known:
            return {'decision': 'known', 'aid': known.get('aid')}

        text = FP.normalize(FP.extract_text(platform, post))
        if not text:
            # 纯图/纯视频无文字：不可能凭文字判重 → 一律按新帖（记日志）
            self._log(platform, pid, uid, ts, 'empty_text', post=post)
            return self._canonical(platform, pid, uid, post, ts, sid=None)
        h = FP.exact_hash(text)
        bag = FP.ngram_bags(text)

        # 1. 未关联账号：入挂起桶，等关联后自动入池
        sid = self.registry.sid_of(platform, uid)
        if not sid:
            self._store_fingerprint(f'acct:{platform}:{uid}', platform, pid, uid, ts, h, bag)
            self._log_unclaimed(platform, pid, uid, ts)
            return {'decision': 'unclaimed'}

        # 1.5 分组开关：该信源未启用判重 → 不比对、不领号、不留指纹（其它池不受影响）
        if not self.registry.dedup_enabled(sid):
            logger.debug(f"[判重] {platform}/{pid} 信源 {sid} 未启用判重，跳过")
            return {'decision': 'dedup_off', 'sid': sid}

        # 2. 逐字级重复：**同池内**哈希命中（跨池永不比较，spec §2.3；
        #    无论时间，spec D2）
        pool_buckets = set(self.registry.buckets_for_sid(sid))
        for rec in self.ledger.exact_index.get(h, []):
            if rec.get('bucket') not in pool_buckets:
                continue
            if rec.get('platform') == platform and str(rec.get('pid')) == str(pid):
                continue                       # 已由步骤0排除，防御
            return self._duplicate(platform, pid, uid, ts, post, sid=sid,
                                   against=rec, reason='exact', score=1.0)

        # 3. 模糊判重：全信源池（sid 桶 + 各成员 acct 桶，同平台多号先汇总
        #    再跨平台比，D20；窗口 |Δt|≤1h，D3）
        buckets = self.registry.buckets_for_sid(sid)
        hit = self._fuzzy_find(buckets, text_bag=bag, ts=ts, platform=platform,
                               pid=pid, post=post)
        if hit:
            score = hit['score']
            return self._duplicate(platform, pid, uid, ts, post, sid=sid,
                                   against=hit['rec'], reason='fuzzy', score=score)
        # 近失段由 _fuzzy_find 内部记账（影子期校准用，不算重复）
        return self._canonical(platform, pid, uid, post, ts, sid=sid, h=h, bag=bag)

    # ---------- 判定实现 ----------

    def _fuzzy_find(self, buckets, *, text_bag, ts, platform: str, pid: str,
                    post: Optional[dict] = None):
        """在同池指纹里找命中（|Δt|≤window_hours，D3）；返回 {'rec','score'} 或 None"""
        lo, hi = ts - 3600 * self.window_hours, ts + 3600 * self.window_hours
        best = None
        near = None
        for rec in self.ledger.bucket_records(buckets):
            if rec.get('platform') == platform and str(rec.get('pid')) == str(pid):
                continue
            rt = rec.get('t') or 0
            if not (lo <= rt <= hi):
                continue
            other_bag = FP.bags_from_json(rec.get('g') or [])
            score = FP.containment(text_bag, other_bag)
            if score >= self.threshold:
                if best is None or score > best['score']:
                    best = {'rec': rec, 'score': score}
            elif score >= self.near_low and (near is None or score > near['score']):
                near = {'rec': rec, 'score': score}
        if near and not best:
            self._log(near['rec'].get('platform'), near['rec'].get('pid'),
                      near['rec'].get('uid'), near['rec'].get('t'),
                      'near_miss', score=round(near['score'], 3),
                      new_platform=platform, new_pid=pid, post=post)
        return best

    def _canonical(self, platform: str, pid: str, uid: str, post: dict, ts: float, *,
                   sid: Optional[int] = None, h: str = '', bag=None) -> dict:
        """新帖 canonical：必领聚合动态号（重复帖不领号，spec D6/D17）。
        未关联(无 sid)走空号路径回报；正文快照一律不存（D17，原始数据在平台 cache）"""
        if not sid:
            return {'decision': 'unclaimed'}
        data_date = datetime.fromtimestamp(ts).strftime('%Y%m%d')
        serial = self.ledger.allocate_serial(sid, data_date)
        aid = make_aggregate_id(sid, data_date, serial)
        rec = _canonical_rec(platform, pid, uid, sid, aid, ts, h)
        self.ledger.put_alias(platform, pid, rec)
        self._store_fingerprint(f'sid:{sid}', platform, pid, uid, ts, h, bag)
        self._log(platform, pid, uid, ts, 'canonical', sid=sid, aid=aid, post=post)
        return {'decision': 'canonical', 'sid': str(sid), 'aid': aid}

    def _duplicate(self, platform: str, pid: str, uid: str, ts: float, post: dict,
                   *, sid: Optional[int] = None, against: dict,
                   reason: str, score: float) -> dict:
        """重复帖：入别名账本 role=alias，不领号、不进指纹库（spec D6/D9/D17）"""
        rec = {'platform': platform, 'pid': str(pid), 'uid': uid,
               'sid': str(sid) if sid else (against.get('sid') or None), 'aid': None, 'role': 'alias', 't': ts,
               'released': False, 'h': against.get('h'),
               'dup_of': {'platform': against.get('platform'),
                          'pid': against.get('pid'),
                          'aid': against.get('aid')},
               'reason': reason, 'score': round(float(score), 3)}
        self.ledger.put_alias(platform, pid, rec)
        self._log(platform, pid, uid, ts, 'duplicate', reason=reason,
                  score=round(float(score), 3),
                  dup_of=rec['dup_of'], post=post)
        return {'decision': 'duplicate', 'reason': reason, 'score': round(float(score), 3)}

    def _store_fingerprint(self, bucket: str, platform: str, pid: str, uid: str,
                           ts: float, h: str, bag):
        if not h:
            return
        rec = {'h': h, 'platform': platform, 'pid': str(pid), 'uid': uid, 't': ts,
               'bucket': bucket}
        if bag is not None:
            rec['g'] = bag
        self.ledger.append_fingerprint(bucket, rec)

    def _log(self, platform, pid, uid, ts, kind: str, *, post=None, text=None,
             **extra):
        """日志事件；post/text 给出时附 80 字内容快照，便于网页端详情预览"""
        if text is None and post:
            fn = _plat_attr(platform, 'display_text')
            if fn is not None:
                try:
                    text = fn(post) or ''
                except Exception:
                    text = ''
            text = text or ((post or {}).get('content')
                            or (post or {}).get('title') or '')
        ev = {'kind': kind, 'platform': platform, 'pid': str(pid),
              'uid': str(uid), 't': ts or time.time()}
        if text:
            ev['text'] = ' '.join(str(text).split())[:80]
        ev.update(extra)
        self.ledger.log_event(ev)

    # ---------- 未关联批量计数日志 ----------

    def _log_unclaimed(self, platform: str, pid: str, uid: str, ts: float):
        key = (platform, str(pid))
        if key in self._unc_seen:          # 同一 pid 整个进程生命周期只计一次
            return
        self._unc_seen.add(key)
        now = time.time()
        if not self._unc_pending:
            self._unc_first = now
        self._unc_pending.setdefault(platform, []).append(str(pid))
        window = 60.0 if not self._unc_flushed_once else 15.0
        if now - self._unc_first >= window:
            self._flush_unclaimed(now)

    def _flush_unclaimed(self, now: float):
        counts = {p: len(v) for p, v in self._unc_pending.items() if v}
        if counts:
            self.ledger.log_event({'kind': 'unclaimed', 'counts': counts})
        self._unc_pending = {}
        self._unc_first = 0.0
        self._unc_flushed_once = True

    # ---------- 放行（spec §7） ----------

    def release(self, platform: str, pid: str) -> Optional[dict]:
        rec = self.ledger.get_alias(platform, str(pid))
        if not rec or rec.get('role') != 'alias':
            return None
        rec['released'] = True
        self.ledger.put_alias(platform, str(pid), rec)
        self._log(platform, pid, rec.get('uid'), rec.get('t'), 'released',
                  dup_of=rec.get('dup_of'))
        return rec

    # ---------- 换信源重发号（D19） ----------

    def reissue_for_account(self, platform: str, uid: str, new_sid: int) -> int:
        """账号被重新关联到另一信源 → 其 canonical 动态按新信源重新领号。

        一帖可有多个聚合id：旧号保留在 rec['aids'] 历史，全部可在
        /data/sd/post/{aid} 反查（dup_of 引用也被覆盖）。返回重发帖数。"""
        new_sid = int(new_sid)
        moved = 0
        targets = [r for r in self.ledger.alias.values()
                   if r.get('role') == 'canonical' and r.get('platform') == platform
                   and str(r.get('uid')) == str(uid) and r.get('sid') != str(new_sid)]
        for rec in targets:
            old_aid = rec.get('aid')
            ts = rec.get('t') or time.time()
            data_date = datetime.fromtimestamp(ts).strftime('%Y%m%d')
            history = list(rec.get('aids') or ([old_aid] if old_aid else []))
            serial = self.ledger.allocate_serial(new_sid, data_date)
            new_aid = make_aggregate_id(new_sid, data_date, serial)
            rec['sid'] = str(new_sid)
            rec['aid'] = new_aid
            rec['aids'] = history + [new_aid]
            self.ledger.put_alias(platform, rec['pid'], rec)
            self._log(platform, rec['pid'], uid, ts, 'reissue',
                      from_sid=rec.get('sid'), to_sid=new_sid,
                      old_aid=old_aid, new_aid=new_aid)
            moved += 1
        return moved

    def lookup_by_aid(self, aid: str) -> Optional[dict]:
        """按任意历史聚合id反查 canonical 记录（D19：一帖多号）"""
        for rec in self.ledger.alias.values():
            if rec.get('role') != 'canonical':
                continue
            if rec.get('aid') == aid or aid in (rec.get('aids') or []):
                return rec
        return None


# ---------- 与平台无关的工具 ----------

def _canonical_rec(platform: str, pid: str, uid: str, sid, aid: str,
                   ts: float, h: str) -> dict:
    """canonical 别名记录（D17：无正文快照，D19：aids 历史链）"""
    return {'platform': platform, 'pid': str(pid), 'uid': uid, 'sid': str(sid),
            'aid': aid, 'aids': [aid], 'role': 'canonical', 't': ts,
            'released': True, 'h': h}


def extract_pid(platform: str, post: dict) -> str:
    """平台帖 ID：取键逻辑在平台插件的 extract_pid hook"""
    fn = _plat_attr(platform, 'extract_pid')
    if fn is not None:
        return fn(post)
    return str(post.get('id') or post.get('pid') or '')
