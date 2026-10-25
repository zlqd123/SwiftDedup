# -*- coding: utf-8 -*-

"""薄账本 — 别名表 / 指纹库 / 计数器 / 判重日志（docs/phase2_spec.md §1）

落盘（均为薄文件，沿用项目"无数据库"风格）：
    data/dedup/alias.json       别名账本 { "<platform>:<pid>": 记录 }
    data/dedup/counters.json    序号计数器 { sid: { "YYYYMMDD": n } }
    data/dedup/fpr/<桶>.jsonl   指纹库（桶键 sid:{sid} 或 acct:{platform}:{uid}）
    data/dedup/dedup_log.jsonl  判重/放行日志（面板读）
    data/dedup/seed_state.json  播种幂等标记

桶模型（spec §3.4 注册表）：涉判重的指纹按"信源池"归桶：
- sid:{sid}            已入信源账号发的帖 + 已关联时的历史播种
- acct:{platform}:{uid} 未关联账号的挂起指纹，关联后它自动并入信源池
  （registry.buckets_for_sid 会同时取 sid 桶与各成员账号的 acct 桶，见 spec §1.1）
"""

import bisect
import json
import os
import time
from pathlib import Path
from collections import Counter, deque
from typing import Dict, List, Optional

from loguru import logger

from .fingerprint import bags_to_json


class Ledger:

    # ---------- 落盘节流：内存态为权威，写盘按时间窗合并（崩溃最多丢最近几秒）----------
    _ALIAS_FLUSH_S = 3.0
    _COUNTERS_FLUSH_S = 10.0
    _LOG_ROTATE_BYTES = 8 * 1024 * 1024   # dedup_log.jsonl 超过 8MB 轮转一份

    def __init__(self, data_dir: str):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fpr_dir = self.dir / 'fpr'
        self.fpr_dir.mkdir(exist_ok=True)

        self.alias_path = self.dir / 'alias.json'
        self.counters_path = self.dir / 'counters.json'
        self.log_path = self.dir / 'dedup_log.jsonl'
        self.seed_state_path = self.dir / 'seed_state.json'

        # 内存态
        self.alias: Dict[str, dict] = {}
        self.counters: Dict[str, Dict[str, int]] = {}
        self.buckets: Dict[str, list] = {}     # 桶键 -> [指纹记录]（按 t 升序）
        self.exact_index: Dict[str, list] = {} # 指纹哈希 -> [指纹记录]
        self._bucket_times: Dict[str, List[float]] = {}  # 与 buckets 对齐的 t 序列（二分用）
        # 查询索引：聚合号 -> canonical 记录；sid -> 名下记录（timeline/计数用）
        self.aid_index: Dict[str, dict] = {}
        self.sid_index: Dict[str, List[dict]] = {}
        # 判重日志内存环形缓冲：SSE/面板高频读取不再全量读文件
        self._log_ring: deque = deque(maxlen=600)
        self._alias_dirty = False
        self._alias_last = 0.0
        self._counters_dirty = False
        self._counters_last = 0.0
        self._log_writes = 0

        self._load()

    # ---------- 生命周期 ----------

    def _load(self):
        if self.alias_path.exists():
            try:
                self.alias = json.loads(self.alias_path.read_text(encoding='utf-8'))
            except Exception as e:
                logger.error(f"alias.json 读取失败，按空账本启动: {e}")
                self.alias = {}
        for rec in self.alias.values():
            self._index_alias(rec)
        if self.counters_path.exists():
            try:
                self.counters = json.loads(self.counters_path.read_text(encoding='utf-8'))
            except Exception as e:
                logger.error(f"counters.json 读取失败，按零计数启动: {e}")
                self.counters = {}
        for f in self.fpr_dir.glob('*.jsonl'):
            recs = []
            for line in f.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    continue
            # 桶键以记录内 bucket 字段为准（文件名冒号转下划线，键不能靠文件名还原）
            for rec in recs:
                bucket = rec.get('bucket') or f.stem
                self.buckets.setdefault(bucket, []).append(rec)
                self.exact_index.setdefault(rec['h'], []).append(rec)
        # 各桶按发布时间排序并建立平行 t 序列（_fuzzy_find 的时间窗二分用）
        for bucket, recs in self.buckets.items():
            recs.sort(key=lambda r: r.get('t') or 0)
            self._bucket_times[bucket] = [r.get('t') or 0 for r in recs]
        # 日志环形缓冲预填充（读一次文件尾部，之后增量走内存）
        for ev in reversed(self._read_log_file(600)):
            self._log_ring.append(ev)
        n = sum(len(v) for v in self.buckets.values())
        logger.info(f"[账本] 别名 {len(self.alias)} 条 / 指纹 {n} 条 "
                    f"/ 计数器 {len(self.counters)} 信源")

    def save_alias(self, force: bool = False):
        if not self._alias_dirty and not force:
            return
        if not force and time.time() - self._alias_last < self._ALIAS_FLUSH_S:
            return
        tmp = self.alias_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.alias, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, self.alias_path)
        self._alias_last = time.time()
        self._alias_dirty = False

    def save_counters(self, force: bool = False):
        if not self._counters_dirty and not force:
            return
        if not force and time.time() - self._counters_last < self._COUNTERS_FLUSH_S:
            return
        tmp = self.counters_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.counters, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, self.counters_path)
        self._counters_last = time.time()
        self._counters_dirty = False

    def flush(self, force: bool = False):
        """把脏账本写盘；main 主循环周期调用（force 供停机兜底）"""
        try:
            self.save_alias(force=force)
            self.save_counters(force=force)
        except Exception as e:
            logger.error(f"[账本] 落盘失败: {e}")

    # ---------- 指纹库 ----------

    def append_fingerprint(self, bucket: str, rec: dict):
        """rec: {h, g(list of [gram,ct]), platform, pid, uid, t}（内存 + jsonl 双写）"""
        rec = dict(rec)
        g = rec.get('g')
        if isinstance(g, Counter):
            rec['g'] = bags_to_json(g)
        else:
            rec['g'] = list(g or [])
        t = rec.get('t') or 0
        recs = self.buckets.setdefault(bucket, [])
        times = self._bucket_times.setdefault(bucket, [])
        # 按 t 有序插入：时间窗查询可二分，不必全桶扫描
        i = bisect.bisect_right(times, t)
        times.insert(i, t)
        recs.insert(i, rec)
        self.exact_index.setdefault(rec['h'], []).append(rec)
        # 桶键含冒号，转成安全文件名
        path = self.fpr_dir / f'{bucket.replace(":", "_")}.jsonl'
        path.parent.mkdir(exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')

    def bucket_records(self, buckets: List[str]) -> List[dict]:
        out = []
        for b in buckets:
            out.extend(self.buckets.get(b) or [])
        return out

    def window_records(self, buckets: List[str], lo: float, hi: float) -> List[dict]:
        """桶内时间窗 [lo, hi] 的指纹记录（各桶已按 t 有序，二分定位）"""
        out = []
        for b in buckets:
            times = self._bucket_times.get(b)
            if not times:
                continue
            i = bisect.bisect_left(times, lo)
            j = bisect.bisect_right(times, hi)
            if j > i:
                out.extend((self.buckets.get(b) or [])[i:j])
        return out

    # ---------- 计数器 ----------

    _COUNTER_KEEP_DAYS = 40   # 每信源只保留最近 N 天的序号计数（展示只用近 3 日）

    def allocate_serial(self, sid: str, date_key: str) -> int:
        """序号每个信源每天从 001 归零（spec D6）；返回已分配的序号"""
        per_day = self.counters.setdefault(str(sid), {})
        per_day[date_key] = int(per_day.get(date_key, 0)) + 1
        # 防无限增长：只留最近 N 天的键（date_key 为 YYYYMMDD，字典序即时间序）
        if len(per_day) > self._COUNTER_KEEP_DAYS:
            for k in sorted(per_day)[:-self._COUNTER_KEEP_DAYS]:
                per_day.pop(k, None)
        self._counters_dirty = True
        self.save_counters()
        return per_day[date_key]

    def counter_status(self, sid: str, days: int = 3) -> dict:
        per_day = self.counters.get(str(sid)) or {}
        keys = sorted(per_day.keys(), reverse=True)[:days]
        return {k: per_day[k] for k in keys}

    # ---------- 别名账本 ----------

    def alias_key(self, platform: str, pid: str) -> str:
        return f'{platform}:{pid}'

    def get_alias(self, platform: str, pid: str) -> Optional[dict]:
        return self.alias.get(self.alias_key(platform, str(pid)))

    @staticmethod
    def _rec_aids(rec: dict) -> List[str]:
        aids = rec.get('aids')
        if not aids:
            aids = [rec['aid']] if rec.get('aid') else []
        return [str(a) for a in aids if a]

    def _index_alias(self, rec: dict):
        if rec.get('role') == 'canonical':
            for aid in self._rec_aids(rec):
                self.aid_index[aid] = rec
        if rec.get('sid') is not None:
            self.sid_index.setdefault(str(rec['sid']), []).append(rec)

    def _unindex_alias(self, rec: dict):
        if rec.get('role') == 'canonical':
            for aid in self._rec_aids(rec):
                cur = self.aid_index.get(aid)
                if cur is rec:
                    self.aid_index.pop(aid, None)
        sid = rec.get('sid')
        if sid is not None:
            lst = self.sid_index.get(str(sid))
            if lst is not None:
                try:
                    lst.remove(rec)
                except ValueError:
                    pass

    def put_alias(self, platform: str, pid: str, rec: dict):
        key = self.alias_key(platform, str(pid))
        old = self.alias.get(key)
        if old is not None:
            self._unindex_alias(old)
        self.alias[key] = rec
        self._index_alias(rec)
        self._alias_dirty = True
        self.save_alias()

    def timeline_for_sid(self, sid: str, limit: int = 50, before_ts: Optional[float] = None,
                         before_pid: Optional[str] = None):
        """信源时间线：canonical 帖 + 已放行的 alias 帖，按 (发布时间, pid) 倒序。

        翻页游标 (before_ts, before_pid)：同秒多帖不再被跳过；
        不带 before_pid 的旧调用保持原先"严格早于 before_ts"的语义。"""
        sid_s = str(sid)
        items = []
        for rec in self.sid_index.get(sid_s, ()):
            if str(rec.get('sid')) != sid_s:      # sid_index 与记录实时字段兜底对齐
                continue
            if rec.get('role') == 'alias' and not rec.get('released'):
                continue
            ts = rec.get('t') or 0
            pid = str(rec.get('pid') or '')
            if before_ts is not None:
                if ts > before_ts:
                    continue
                # 倒序列表（(t, pid) 降序）：游标之后 = ts 更小，或同秒且 pid 更小
                if ts == before_ts and (before_pid is None or pid >= str(before_pid)):
                    continue
            items.append(rec)
        items.sort(key=lambda r: ((r.get('t') or 0), str(r.get('pid') or '')), reverse=True)
        return items[:limit]

    # ---------- 播种 ----------

    def seed_state(self) -> dict:
        if self.seed_state_path.exists():
            try:
                return json.loads(self.seed_state_path.read_text(encoding='utf-8'))
            except Exception:
                pass
        return {}

    def set_seed_state(self, state: dict):
        tmp = self.seed_state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, self.seed_state_path)

    # ---------- 日志 ----------

    def log_event(self, event: dict):
        event.setdefault('time', time.time())
        self._log_ring.append(event)
        self._log_writes += 1
        try:
            # 超过阈值轮转一份（dedup_log.jsonl.1），防长期运行无限膨胀
            if (self._log_writes % 256 == 0 and self.log_path.exists()
                    and self.log_path.stat().st_size > self._LOG_ROTATE_BYTES):
                os.replace(self.log_path, self.log_path.with_name(self.log_path.name + '.1'))
        except OSError:
            pass
        with open(self.log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')

    def _read_log_file(self, limit: int) -> List[dict]:
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text(encoding='utf-8').splitlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return list(reversed(out))

    def read_log(self, limit: int = 200) -> List[dict]:
        """近 limit 条日志（新→旧）。limit 在环形缓冲内直接走内存，不再读文件。"""
        if limit <= len(self._log_ring):
            return list(self._log_ring)[-limit:][::-1]
        return self._read_log_file(limit)
