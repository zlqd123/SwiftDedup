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

import json
import os
from pathlib import Path
from collections import Counter
from typing import Dict, List, Optional

from loguru import logger

from .fingerprint import bags_to_json


class Ledger:

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
        self.buckets: Dict[str, list] = {}     # 桶键 -> [指纹记录]
        self.exact_index: Dict[str, list] = {} # 指纹哈希 -> [指纹记录]

        self._load()

    # ---------- 生命周期 ----------

    def _load(self):
        if self.alias_path.exists():
            try:
                self.alias = json.loads(self.alias_path.read_text(encoding='utf-8'))
            except Exception as e:
                logger.error(f"alias.json 读取失败，按空账本启动: {e}")
                self.alias = {}
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
        n = sum(len(v) for v in self.buckets.values())
        logger.info(f"[账本] 别名 {len(self.alias)} 条 / 指纹 {n} 条 "
                    f"/ 计数器 {len(self.counters)} 信源")

    def save_alias(self):
        tmp = self.alias_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.alias, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, self.alias_path)

    def save_counters(self):
        tmp = self.counters_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.counters, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, self.counters_path)

    # ---------- 指纹库 ----------

    def append_fingerprint(self, bucket: str, rec: dict):
        """rec: {h, g(list of [gram,ct]), platform, pid, uid, t}（内存 + jsonl 双写）"""
        rec = dict(rec)
        g = rec.get('g')
        if isinstance(g, Counter):
            rec['g'] = bags_to_json(g)
        else:
            rec['g'] = list(g or [])
        self.buckets.setdefault(bucket, []).append(rec)
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

    # ---------- 计数器 ----------

    def allocate_serial(self, sid: str, date_key: str) -> int:
        """序号每个信源每天从 001 归零（spec D6）；返回已分配的序号"""
        per_day = self.counters.setdefault(str(sid), {})
        per_day[date_key] = int(per_day.get(date_key, 0)) + 1
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

    def put_alias(self, platform: str, pid: str, rec: dict):
        self.alias[self.alias_key(platform, str(pid))] = rec
        self.save_alias()

    def timeline_for_sid(self, sid: str, limit: int = 50, before_ts: Optional[float] = None):
        """信源时间线：canonical 帖 + 已放行的 alias 帖，按发布时间倒序"""
        items = []
        for rec in self.alias.values():
            if rec.get('sid') != str(sid) and rec.get('sid') != sid:
                continue
            if rec.get('role') == 'alias' and not rec.get('released'):
                continue
            ts = rec.get('t') or 0
            if before_ts and ts >= before_ts:
                continue
            items.append(rec)
        items.sort(key=lambda r: r.get('t') or 0, reverse=True)
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
        event.setdefault('time', __import__('time').time())
        with open(self.log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')

    def read_log(self, limit: int = 200) -> List[dict]:
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
