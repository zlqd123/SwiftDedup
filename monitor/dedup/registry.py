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
        """sources.yaml + sources.json 双层合并

        合并规则（修复：UI 改动必须对 yaml 定义的信源也生效，且重启不回弹）：
        - 运行层按**平台级列表**覆盖 yaml：某平台 key 出现在运行层记录（含显式空
          列表，表达"从 yaml 移除该平台全部账号"）即以运行层为准；
        - 运行层墓碑（deleted: true）屏蔽 yaml 里已被 UI 删除的信源；
        - 未被运行层触碰的平台/字段继续跟随 sources.yaml 编辑（yaml 仍是权威底稿）。
        """
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

        tombstones = set()
        if self.runtime_path.exists():
            try:
                self._runtime = json.loads(self.runtime_path.read_text(encoding='utf-8'))
            except Exception as e:
                logger.error(f"sources.json 解析失败: {e}")
                self._runtime = {'sources': []}
            for x in (self._runtime.get('sources') or []):
                if (x or {}).get('deleted'):
                    try:
                        tombstones.add(int(x.get('id')))
                    except (TypeError, ValueError):
                        continue
                entries.append(('runtime', x))
            # 运行层待认领（解绑/删信源回列的账号，跨重启保留）
            un = (self._runtime.get('unclaimed') or {})
            for p in _plat_names():
                for uid in (un.get(p) or []):
                    self._unclaimed[p].append({'uid': str(uid), 'from': 'runtime'})

        seen = {}
        for layer, entry in entries:
            entry = entry or {}
            sid_raw = str(entry.get('id') or '').strip()
            if not sid_raw:
                continue
            try:
                sid = int(sid_raw)
            except ValueError:
                logger.warning(f"信源 id '{sid_raw}' 非数字，忽略")
                continue
            if sid in tombstones:
                continue
            name = str(entry.get('name') or '').strip()
            accounts = entry.get('accounts') or {}
            # 以已知平台名为准归一化；未知平台 key 一并保留 —— 否则"平台插件
            # 尚未 import 的进程"(离线探针/工具脚本)会静默丢失整组账号映射。
            normalized = {}
            for p, v in accounts.items():
                uids = []
                for u in (v or []):
                    u = str(u).strip()
                    if u:
                        uids.append(u)
                # 运行层保留显式空列表（表达"从 yaml 移除该平台全部账号"）
                if uids or layer == 'runtime':
                    normalized[p] = uids
            if not normalized and layer != 'runtime':
                # 手写 yaml 里无账号的空占位跳过；运行层(UI 新建、尚未绑账号)必须保留
                continue
            rec = {'sid': sid, 'name': name or f'信源{sid}', 'accounts': normalized,
                   'dedup': bool(entry.get('dedup', True))}
            if sid in seen:                           # 运行层覆盖 yaml（平台级列表）
                prev = seen[sid]
                rec = {**prev, 'accounts': {**prev.get('accounts', {}), **normalized}}
                if name:
                    rec['name'] = name
                # dedup 只有运行层显式给出才覆盖（运行层记录现在总是带该字段）
                if layer == 'runtime' and 'dedup' in entry:
                    rec['dedup'] = bool(entry.get('dedup'))
            seen[sid] = rec
        # 合并后收敛：显式空列表最终表示"该平台无账号"
        for rec in seen.values():
            rec['accounts'] = {p: u for p, u in rec['accounts'].items() if u}
        for sid, rec in seen.items():
            self._sources[sid] = rec

        self._index = {}
        for sid, rec in self._sources.items():
            for p, uids in rec['accounts'].items():
                for uid in uids:
                    key = (p, uid)
                    if key in self._index and self._index[key] != sid:
                        logger.warning(f"[信源] 账号 {p}/{uid} 同时定义在信源 "
                                       f"{self._index[key]} 与 {sid}，以先者为准")
                        continue
                    self._index[key] = sid

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

    def dedup_enabled(self, sid) -> bool:
        """该信源是否启用聚合判重（分组开关，缺省 true 保持全局旧行为）"""
        rec = self._sources.get(int(sid)) if sid is not None else None
        return bool(rec.get('dedup', True)) if rec else False

    def set_dedup(self, sid, flag: bool) -> bool:
        """设置信源级判重开关并持久化（运行层）；不影响其它信源"""
        rec = self._sources.get(int(sid))
        if not rec:
            return False
        rec['dedup'] = bool(flag)
        self._runtime_override(sid, None)
        return True

    def next_free_sid(self) -> int:
        """自动顺编：从 10001 起第一个空闲号（手动小号 1-9999 段不自动占用）"""
        used = set(self._sources)
        n = 10001
        while n in used:
            n += 1
        return n

    # ---------- 变更（运行期，持久化到 sources.json） ----------

    def _runtime_override(self, sid, platform):
        """把一次 UI 变更写入运行层（sources.json）。

        只覆盖被触碰的平台级列表 / 信源级字段（name、dedup），未触碰的继续跟随
        sources.yaml —— 运行层显式空列表表示"从 yaml 移除该平台全部账号"，
        由此换源/解绑/删除对 yaml 定义的信源同样生效且重启不回弹。"""
        sid = int(sid)
        rec = self._sources.get(sid)
        entry = next((r for r in (self._runtime.get('sources') or [])
                      if r.get('id') == sid and not r.get('deleted')), None)
        if entry is None:
            entry = {'id': sid,
                     'name': (rec or {}).get('name') or f'信源{sid}',
                     'accounts': {},
                     'dedup': bool((rec or {}).get('dedup', True))}
            self._runtime.setdefault('sources', []).append(entry)
        if platform is not None:
            cur = list(((rec or {}).get('accounts') or {}).get(platform) or [])
            entry['accounts'][platform] = cur
        elif rec is not None:
            entry['name'] = rec.get('name') or entry.get('name') or f'信源{sid}'
            entry['dedup'] = bool(rec.get('dedup', True))
        self._persist_runtime()

    def _persist_runtime(self):
        # 待认领随每次变更同步落盘（解绑/删信源回列的账号跨重启保留）
        self._runtime['unclaimed'] = {p: sorted({r['uid'] for r in v})
                                      for p, v in self._unclaimed.items() if v}
        self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.runtime_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self._runtime, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.runtime_path)

    def upsert_link(self, platform: str, uid: str, sid: int) -> bool:
        """把账号挂到信源（允许同平台多号）；sid 不存在则要求先建源。
        换源会把新旧两个信源的平台级列表都写入运行层，重启不回弹。"""
        platform, uid, sid = platform, str(uid), int(sid)
        if not self.has_sid(sid):
            return False
        # 从其它信源/待认领移出
        old_sid = self._index.get((platform, uid))
        if old_sid is not None and old_sid != sid:
            other = self._sources.get(old_sid)
            if other is not None:
                lst = other['accounts'].get(platform) or []
                if uid in lst:
                    lst.remove(uid)
        self._sources[sid]['accounts'].setdefault(platform, [])
        if uid not in self._sources[sid]['accounts'][platform]:
            self._sources[sid]['accounts'][platform].append(uid)
        self._unclaimed[platform] = [r for r in self._unclaimed.get(platform, [])
                                     if r['uid'] != uid]
        self._runtime_override(sid, platform)
        if old_sid is not None and old_sid != sid:
            self._runtime_override(old_sid, platform)
        self._index[(platform, uid)] = sid
        return True

    def unlink(self, platform: str, uid: str, sid: int) -> bool:
        """解绑账号。运行层写入显式（可能为空的）平台列表 —— yaml 定义的账号移除同样生效。"""
        platform, uid, sid = platform, str(uid), int(sid)
        rec = self._sources.get(sid)
        if not rec:
            return False
        lst = rec['accounts'].get(platform) or []
        if uid not in lst:
            return False
        lst.remove(uid)
        self._index.pop((platform, uid), None)
        self._unclaimed.setdefault(platform, []).append({'uid': uid, 'from': 'runtime'})
        self._runtime_override(sid, platform)   # 落盘时同步带上待认领列表
        return True

    def create_source(self, sid: Optional[int] = None, name: str = '') -> int:
        sid = sid if sid else self.next_free_sid()
        sid = int(sid)
        if sid in self._sources:
            raise ValueError(f'sid {sid} 已存在')
        rec = {'sid': sid, 'name': name or f'信源{sid}', 'accounts': {}}
        self._sources[sid] = rec
        self._runtime.setdefault('sources', []).append(
            {'id': sid, 'name': rec['name'], 'accounts': {}, 'dedup': True})
        self._persist_runtime()
        return sid

    def delete_source(self, sid: int) -> bool:
        """删除信源；其下已关联账号回到待认领。自动号段不回收。
        运行层写墓碑（deleted）：yaml 定义的信源删除后重启不再复活。"""
        rec = self._sources.get(int(sid))
        if not rec:
            return False
        for (p, u), sv in list(self._index.items()):
            if sv == int(sid):
                self._index.pop((p, u), None)
                self._unclaimed.setdefault(p, []).append({'uid': u, 'from': 'runtime'})
        del self._sources[int(sid)]
        self._runtime['sources'] = [r for r in (self._runtime.get('sources') or [])
                                    if r.get('id') != int(sid)]
        self._runtime['sources'].append({'id': int(sid), 'deleted': True})
        self._persist_runtime()
        return True

    def rename_source(self, sid: int, name: str) -> bool:
        rec = self._sources.get(int(sid))
        if not rec:
            return False
        rec['name'] = name
        self._runtime_override(sid, None)
        return True
