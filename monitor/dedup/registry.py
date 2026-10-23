# -*- coding: utf-8 -*-

"""信源注册表 — sources.yaml 手动编写的 sid/name/account 分组 + 运行期增量

双层数据：
- sources.yaml      使用者手动编写的分组与 sid/name（权威，不回写）
- data/sources.json 运行期增量（面板 PATCH 建立的关联/新建信源），合并优先

分组语义（spec §1 / D7）：一个 sid = 一个判重池；同一平台可挂多个账号；
accounts.{platform} 恒为列表。id/name 为空的条目视为"草案未生效"。
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml
from loguru import logger

from ..core.platform import names as _plat_names

from ..core.platform import names as _plat_names



class SourceRegistry:

    def __init__(self, sources_yaml_path: str, data_dir: str):
        self.yaml_path = Path(sources_yaml_path)
        self.runtime_path = Path(data_dir) / 'sources.json'
        self._sources: Dict[str, dict] = {}       # sid(大写无前导零原样) -> {sid:int, name, accounts:{platform:[uid]}}
        self._index: Dict[Tuple[str, str], str] = {}  # (platform, uid) -> sid
        self._unclaimed: Dict[str, List[dict]] = {p: [] for p in _plat_names()}
        self._runtime: dict = {'sources': []}     # 运行期新增/修改（JSON 持久化）
        self.stamp_snapshot: dict = {}            # (yaml/runtime) mtime 快照 → 手工编辑热重载
        self.reload()

    # ---------- 加载 ----------

    def check_reload(self):
        """防抖检查：sources.yaml / sources.json 手工编辑 → 自动 reload（mtime 两次 stat，轻）"""
        try:
            ys = self.yaml_path.stat().st_mtime if self.yaml_path.exists() else 0
            rs = self.runtime_path.stat().st_mtime if self.runtime_path.exists() else 0
        except OSError:
            return False
        if self.stamp_snapshot.get('y') == ys and self.stamp_snapshot.get('r') == rs:
            return False
        self.stamp_snapshot = {'y': ys, 'r': rs}
        self.reload()
        return True

    def reload(self):
        """sources.yaml + sources.json 双层合并（两层并存，manual 不被运行态覆盖）"""
        self._sources, self._index = {}, {}
        self._unclaimed = {p: [] for p in _plat_names()}

        entries = []
        if self.yaml_path.exists():
            try:
                doc = yaml.safe_load(self.yaml_path.read_text(encoding='utf-8')) or {}
                entries.extend(('yaml', x) for x in (doc.get('sources') or []))
                # yaml 中显式列出的待认领账号也登记（用户可手动整理）
                un = doc.get('unclaimed') or {}
                for p in _plat_names():
                    for uid in (un.get(p) or []):
                        self._unclaimed[p].append({'uid': str(uid),
                                                   'from': 'yaml'})
            except Exception as e:
                logger.error(f"sources.yaml 解析失败: {e}")

        if self.runtime_path.exists():
            try:
                self._runtime = json.loads(self.runtime_path.read_text(encoding='utf-8'))
                entries.extend(('runtime', x) for x in (self._runtime.get('sources') or []))
                un = (self._runtime.get('unclaimed') or {})
                for p in _plat_names():
                    for uid in (un.get(p) or []):
                        self._unclaimed[p].append({'uid': str(uid), 'from': 'runtime'})
            except Exception as e:
                logger.error(f"sources.json 解析失败: {e}")

        # 运行期条目优先（同 sid 覆盖 yaml 草案），未填 sid/name 的跳过
        seen = {}
        for layer, entry in entries:
            sid_raw = str((entry or {}).get('id') or '').strip()
            if not sid_raw:
                continue
            try:
                sid = int(sid_raw)
            except ValueError:
                logger.warning(f"信源 id '{sid_raw}' 非数字，忽略")
                continue
            name = str((entry or {}).get('name') or '').strip()
            accounts = entry.get('accounts') or {}
            normalized = {}
            for p in _plat_names():
                v = accounts.get(p)
                uids = []
                for u in (v or []):
                    u = str(u).strip()
                    if u:
                        uids.append(u)
                if uids:
                    normalized[p] = uids
            if not normalized and layer != 'runtime':
                # 手写 yaml 里无账号的空占位跳过；运行层(UI 新建、尚未绑账号)必须保留
                continue
            rec = {'sid': sid, 'name': name or f'信源{sid}', 'accounts': normalized}
            if sid in seen:                           # 后者覆盖前者（runtime 优先）
                rec = {**seen[sid], 'accounts': {**seen[sid].get('accounts', {}), **normalized}}
            seen[sid] = rec
        runtime_now = [{'id': sid, 'name': rec['name'],
                        'accounts': rec['accounts']} for sid, rec in seen.items()]
        if (self._runtime.get('sources') or []) != runtime_now:
            self._runtime['sources'] = runtime_now
            self._persist_runtime()
        for sid, rec in seen.items():
            self._sources[sid] = rec

        self._index = {}
        for sid, rec in self._sources.items():
            for p, uids in rec['accounts'].items():
                for uid in uids:
                    self._index[(p, uid)] = sid

        # 待认领去重
        for p in _plat_names():
            uniq, seen_u = [], set()
            for r in self._unclaimed[p]:
                u = r['uid']
                if u in seen_u or (p, u) in self._index:
                    continue
                seen_u.add(u)
                uniq.append(r)
            self._unclaimed[p] = uniq

        logger.info(f"[信源] 加载 {len(self._sources)} 个信源 / "
                    "待认领 " + " ".join(f"{p}={len(v)}" for p, v in self._unclaimed.items()))

    # ---------- 查询 ----------

    def sid_of(self, platform: str, uid: str) -> Optional[int]:
        return self._index.get((platform, str(uid)))

    def source(self, sid: int) -> Optional[dict]:
        return self._sources.get(int(sid))

    def all_sources(self) -> List[dict]:
        return sorted(self._sources.values(), key=lambda r: r['sid'])

    def unclaimed(self) -> dict:
        return {p: list(v) for p, v in self._unclaimed.items()}

    def buckets_for_sid(self, sid: int) -> List[str]:
        """该信源的指纹池桶键（spec §1：sid 桶 + 各成员账号的未认领桶）"""
        rec = self._sources.get(int(sid))
        if not rec:
            return [f'sid:{sid}']
        keys = [f"sid:{sid}"]
        for p, uids in rec['accounts'].items():
            for uid in uids:
                keys.append(f'acct:{p}:{uid}')
        return keys

    def has_sid(self, sid: int) -> bool:
        return int(sid) in self._sources

    def next_free_sid(self) -> int:
        """自动顺编：从 10001 起第一个空闲号（手动小号 1-9999 段不自动占用）"""
        used = set(self._sources)
        n = 10001
        while n in used:
            n += 1
        return n

    # ---------- 变更（运行期，持久化到 sources.json） ----------

    def _persist_runtime(self):
        self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.runtime_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self._runtime, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.runtime_path)

    def upsert_link(self, platform: str, uid: str, sid: int) -> bool:
        """把账号挂到信源（允许同平台多号）；sid 不存在则要求先建源"""
        platform, uid, sid = platform, str(uid), int(sid)
        if not self.has_sid(sid):
            return False
        # 从其它信源/待认领移出
        for other in self._sources.values():
            lst = other['accounts'].get(platform) or []
            if uid in lst:
                lst.remove(uid)
        self._sources[sid]['accounts'].setdefault(platform, [])
        if uid not in self._sources[sid]['accounts'][platform]:
            self._sources[sid]['accounts'][platform].append(uid)
        self._unclaimed.setdefault(platform, [])
        self._unclaimed.setdefault(platform, [])
        self._unclaimed[platform] = [r for r in self._unclaimed[platform] if r['uid'] != uid]
        self._runtime['sources'] = [r for r in (self._runtime.get('sources') or [])
                                    if r.get('id') != sid]
        self._runtime['sources'].append({'id': sid,
                                         'name': self._sources[sid]['name'],
                                         'accounts': dict(self._sources[sid]['accounts'])})
        self._index[(platform, uid)] = sid
        self._persist_runtime()
        return True

    def unlink(self, platform: str, uid: str, sid: int) -> bool:
        platform, uid, sid = platform, str(uid), int(sid)
        rec = self._sources.get(sid)
        if not rec:
            return False
        lst = rec['accounts'].get(platform) or []
        if uid not in lst:
            return False
        lst.remove(uid)
        self._runtime['sources'] = [r for r in (self._runtime.get('sources') or [])
                                    if r.get('id') != sid]
        self._runtime['sources'].append({'id': sid, 'name': rec['name'],
                                         'accounts': dict(rec['accounts'])})
        self._index.pop((platform, uid), None)
        self._unclaimed.setdefault(platform, []).append({'uid': uid, 'from': 'runtime'})
        self._persist_runtime()
        return True

    def create_source(self, sid: Optional[int] = None, name: str = '') -> int:
        sid = sid if sid else self.next_free_sid()
        sid = int(sid)
        if sid in self._sources:
            raise ValueError(f'sid {sid} 已存在')
        rec = {'sid': sid, 'name': name or f'信源{sid}', 'accounts': {}}
        self._sources[sid] = rec
        self._runtime['sources'].append({'id': sid, 'name': rec['name'], 'accounts': {}})
        self._persist_runtime()
        return sid

    def delete_source(self, sid: int) -> bool:
        """删除信源（运行层）；其下已关联账号回到待认领。自动号段不回收。"""
        rec = self._sources.get(int(sid))
        if not rec:
            return False
        for (p, u), sv in list(self._index.items()):
            if sv == int(sid):
                self._index.pop((p, u), None)
                self._unclaimed.setdefault(p, []).append({'uid': u, 'from': 'runtime'})
        del self._sources[int(sid)]
        self._runtime['sources'] = [r for r in (self._runtime.get('sources') or [])
                                    if r.get('id') != sid]
        self._persist_runtime()
        return True

    def rename_source(self, sid: int, name: str) -> bool:
        rec = self._sources.get(int(sid))
        if not rec:
            return False
        rec['name'] = name
        self._runtime['sources'] = [r for r in (self._runtime.get('sources') or [])
                                    if r.get('id') != int(sid)]
        self._runtime['sources'].append({'id': int(sid), 'name': name,
                                         'accounts': dict(rec['accounts'])})
        self._persist_runtime()
        return True
