# -*- coding: utf-8 -*-

"""播种器 — 把历史缓存提炼成指纹，历史仅学习（判重设计规范 §3）

范围（D11）：三处旧缓存 → 仅指纹记录（平台+pid+时间+哈希+n-gram），
不发聚合号、不建别名、不出现在任何端点。全部进入 acct 挂起桶：
关联与否不影响其实际归属（registry.buckets_for_sid 同时取成员账号桶）。

幂等：seed_state.json 记载已播种的 pid 集合摘要；重跑自动跳过已量化条目。
"""

import json
import time
from pathlib import Path
from typing import Dict, Iterable

from loguru import logger

from . import fingerprint as FP
from ..core.platform import attr as _plat_attr
from .engine import DedupEngine, extract_pid

SEED_VERSION = 1


class Seeder:

    def __init__(self, engine: DedupEngine, seeds_cfg: list):
        """seeds_cfg: config.dedup.seeds = [ {platform: <平台名>, path: ...}, ... ]"""
        self.engine = engine
        self.ledger = engine.ledger
        self.seeds = seeds_cfg or []

    async def run(self, force: bool = False) -> dict:
        state = self.ledger.seed_state()
        if state.get('done') and not force:
            logger.info(f"[播种] 已于之前完成（{state.get('counts')}），跳过")
            return state

        counts: Dict[str, int] = {}
        seen_pids = set()
        # 桶内已播种 pid 集合：O(1) 查重（原为逐条线性扫桶，历史缓存大时 O(n²)）
        seeded = {(b, str(r.get('pid')))
                  for b, recs in self.ledger.buckets.items() for r in recs}
        for cfg in self.seeds:
            platform = str(cfg.get('platform') or '').strip()
            path = Path(str(cfg.get('path') or ''))
            it_fn = _plat_attr(platform, 'iter_seed_cache')
            if it_fn is None or not path.exists():
                logger.warning(f"[播种] 跳过无效配置（无该平台插件或路径缺失）: {cfg}")
                continue
            n = added = 0
            for plat, uid, post in it_fn(path):
                pid = extract_pid(plat, post)
                if not pid or (plat, uid, pid) in seen_pids:
                    continue
                seen_pids.add((plat, uid, pid))
                n += 1
                if self._seed_one(plat, uid, post, pid, seeded):
                    added += 1
            counts[f'{platform}@{path}'] = added
            logger.info(f"[播种] {platform} {path} 扫到 {n} 条 → 新增指纹 {added}")

        state = {'done': time.time(), 'version': SEED_VERSION, 'counts': counts}
        self.ledger.set_seed_state(state)
        self.ledger.log_event({'kind': 'seed_done', 'counts': counts})
        total = sum(counts.values())
        logger.info(f"[播种] 完成：共 {total} 条历史指纹（仅学习，不发号）")
        return state

    def _seed_one(self, platform: str, uid: str, post: dict, pid: str,
                  seeded: set) -> bool:
        # 抽奖公示等"直接不推"内容（D22）：历史也不播种，与运行时形态一致
        lf = _plat_attr(platform, 'lottery_filtered')
        if lf is not None and lf(post):
            return False
        bucket = f'acct:{platform}:{uid}'
        # 同桶内同 pid 一律跳过（跨代缓存重叠/文本编辑演化都不重播）
        if (bucket, str(pid)) in seeded:
            return False
        text = FP.normalize(FP.extract_text(platform, post))
        h = FP.exact_hash(text) if text else ''
        if h:
            # 全库哈希同 pid 重复（跨目录同帖）也合并
            for rec in self.ledger.exact_index.get(h, []):
                if rec.get('platform') == platform and str(rec.get('pid')) == str(pid):
                    return False
        ts = DedupEngine.publish_ts(platform, post)
        bag = FP.ngram_bags(text) if text else None
        self.engine._store_fingerprint(bucket, platform, pid, uid, ts, h, bag)
        seeded.add((bucket, str(pid)))
        return True
