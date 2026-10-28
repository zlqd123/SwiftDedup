#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""审查修复回归测试（无 pytest 依赖，直接运行：python tests/test_fixes.py）

覆盖 2026-10 审查修复的关键路径：
1. adfilter._build_history 历史学习（曾因元组解包顺序错误完全失效）
2. registry 运行层覆盖语义：换源/解绑/删除对 yaml 定义的信源生效、
   dedup 开关不因改名/换源丢失、重启（reload）不回弹
3. ledger 内存索引：aid 反查、同秒复合游标翻页不漏帖
4. engine 换信源重发号日志的 from_sid 字段（曾被覆盖成 to_sid）
5. engine 模糊判重时间窗（window_records 二分路径）与逐字判重
6. ledger 日志环形缓冲读取代全量文件读
"""

import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor.dedup.ledger import Ledger
from monitor.dedup.registry import SourceRegistry
from monitor.dedup.engine import DedupEngine

PASS = []


def check(name, cond, detail=''):
    if cond:
        PASS.append(name)
        print(f'  ✓ {name}')
    else:
        print(f'  ✗ {name} {detail}')
        raise AssertionError(name)


# ---------------------------------------------------------------- 1. adfilter

def test_adfilter_history(tmp: Path):
    print('[1] adfilter 冷启动历史学习')
    # platform/ 与标准库 platform 同名，须按插件加载方式取得 sdplat 包再导入；
    # 主仓库单独 checkout（无 platform/ 插件仓）时跳过本组
    root = Path(__file__).resolve().parents[1]
    if not (root / 'platform' / 'plugin.py').exists():
        print('  - 跳过（未安装 platform/ 插件仓）')
        return
    from monitor.core.platform import load_plugins
    load_plugins(root / 'platform')
    from sdplat.weibo.adfilter import AdFilter, match_key
    cache = tmp / 'wcache'
    tpl_lines = ['————', '新版本上线啦', '详情见 https://t.cn/xxx']
    for i, wid in enumerate(['w1', 'w2', 'w3']):
        d = cache / 'user_777' / wid
        d.mkdir(parents=True)
        text = f'正文第{i}条\n' + '\n'.join(tpl_lines)
        (d / f'{wid}.json').write_text(
            json.dumps({'idstr': wid, 'timestamp': 1700000000 + i,
                        'text_raw': text}), encoding='utf-8')
    f = AdFilter(str(cache), {'state_file': str(tmp / 'af_state.json')})
    f._build_history('777')
    lf = f._line_freq['777']
    key = match_key('新版本上线啦')
    check('历史行频次学到了正文模板行', lf.get(key, 0) >= 2,
          f'got {lf.get(key)}')
    check('目录名不再混入行频次', match_key('w1') not in lf)
    check('observed 记录的是 wid',
          {'w1', 'w2', 'w3'} <= f._observed.get('777', set()),
          str(f._observed.get('777')))


# ---------------------------------------------------------------- 2. registry

def test_registry_runtime_overrides(tmp: Path):
    print('[2] registry 运行层覆盖语义')
    tmp.mkdir(parents=True, exist_ok=True)
    yml = tmp / 'sources.yaml'
    yml.write_text(
        'sources:\n'
        '  - id: "1"\n    name: 舟系\n'
        '    accounts:\n      weibo: ["a", "b"]\n'
        '  - id: "2"\n    name: 终系\n'
        '    accounts:\n      weibo: ["c"]\n'
        '  - id: "3"\n    name: 待删\n'
        '    accounts:\n      bili: ["z"]\n',
        encoding='utf-8')
    reg = SourceRegistry(str(yml), str(tmp / 'reg'))
    check('初始归属', reg.sid_of('weibo', 'a') == 1)

    # a 从信源1换到信源2
    reg.upsert_link('weibo', 'a', 2)
    reg.reload()   # 模拟重启
    check('换源后 a 归属信源2（reload 不回弹）', reg.sid_of('weibo', 'a') == 2,
          f"got {reg.sid_of('weibo', 'a')}")
    check('信源1不再列出 a',
          'a' not in (reg.source(1)['accounts'].get('weibo') or []))

    # 解绑 yaml 定义的 b
    reg.unlink('weibo', 'b', 1)
    reg.reload()
    check('解绑 yaml 账号后不复活', reg.sid_of('weibo', 'b') is None)
    check('解绑账号回到待认领',
          any(r['uid'] == 'b' for r in reg.unclaimed().get('weibo', [])))

    # dedup 开关保持：关掉 → 改名 → reload
    reg.set_dedup(1, False)
    reg.rename_source(1, '舟系改名')
    reg.reload()
    check('改名后 dedup=False 不丢', reg.dedup_enabled(1) is False,
          f"got {reg.dedup_enabled(1)}")

    # 删除 yaml 定义的信源3
    reg.delete_source(3)
    reg.reload()
    check('删除 yaml 信源后不复活', reg.source(3) is None)
    check('被删信源账号回待认领',
          any(r['uid'] == 'z' for r in reg.unclaimed().get('bili', [])))

    # UI 新建信源 + 换回：runtime-only 信源正常
    sid = reg.create_source(name='新池')
    reg.upsert_link('weibo', 'b', sid)
    reg.reload()
    check('新建信源挂账号（reload 存活）', reg.sid_of('weibo', 'b') == sid)


# ---------------------------------------------------------------- 3. ledger

def test_ledger_indexes(tmp: Path):
    print('[3] ledger 索引与复合游标')
    tmp.mkdir(parents=True, exist_ok=True)
    led = Ledger(str(tmp / 'dedup'))
    rec1 = {'platform': 'p', 'pid': '1', 'sid': '7', 'aid': 'A1', 'aids': ['A1'],
            'role': 'canonical', 't': 100.0, 'released': True}
    rec2 = {'platform': 'p', 'pid': '2', 'sid': '7', 'aid': 'A2', 'aids': ['A2'],
            'role': 'canonical', 't': 100.0, 'released': True}
    rec3 = {'platform': 'p', 'pid': '3', 'sid': '7', 'aid': 'A3', 'aids': ['A3'],
            'role': 'canonical', 't': 200.0, 'released': True}
    for r in (rec1, rec2, rec3):
        led.put_alias('p', r['pid'], r)
    check('aid 反查 O(1)', led.aid_index.get('A2') is rec2)
    # 同秒（t=100）两条：旧游标（仅 ts）会漏一条，复合游标不漏
    page1 = led.timeline_for_sid(7, limit=1, before_ts=200.0)
    check('翻页第一页命中同秒组', page1 and page1[0]['pid'] in ('1', '2'))
    nxt_ts, nxt_pid = page1[0]['t'], str(page1[0]['pid'])
    page2 = led.timeline_for_sid(7, limit=10, before_ts=nxt_ts, before_pid=nxt_pid)
    check('复合游标翻页拿到同秒另一条', len(page2) == 1
          and page2[0]['pid'] in ('1', '2') and page2[0]['pid'] != nxt_pid)
    # 覆盖写入时索引维护：改 rec1 的 aid
    rec1b = dict(rec1, aid='A1b', aids=['A1', 'A1b'])
    led.put_alias('p', '1', rec1b)
    check('旧聚合号仍在 aids 历史可反查', led.aid_index.get('A1') is rec1b)
    check('新聚合号可反查', led.aid_index.get('A1b') is rec1b)
    # 计数器裁剪
    for day in range(1, 60):
        led.allocate_serial(7, f'202610{day:02d}')
    check('计数器按天裁剪', len(led.counters['7']) <= led._COUNTER_KEEP_DAYS)
    led.flush(force=True)
    led2 = Ledger(str(tmp / 'dedup'))
    check('节流落盘后重启数据完整',
          led2.aid_index.get('A2') is not None and led2.get_alias('p', '3') is not None)


# ---------------------------------------------------------------- 4-5. engine

async def test_engine(tmp: Path):
    print('[4] engine 重发号/判重')
    tmp.mkdir(parents=True, exist_ok=True)
    yml = tmp / 'esources.yaml'
    yml.write_text('sources: []\n', encoding='utf-8')
    reg = SourceRegistry(str(yml), str(tmp / 'ereg'))
    reg.create_source(sid=1, name='池一')
    reg.create_source(sid=2, name='池二')
    reg.upsert_link('weibo', 'u1', 1)
    led = Ledger(str(tmp / 'ededup'))
    cfg = SimpleNamespace(dedup={'enabled': True, 'fuzzy_threshold': 0.6,
                                 'window_hours': 1.0, 'near_miss_low': 0.4})
    eng = DedupEngine(cfg, reg, led)
    eng.publish_ts = lambda platform, post: float(post['ts'])   # 测试用可控时间

    base = time.time() - 3600
    text = '这是跨平台完全一致的一条动态内容，用于验证判重引擎。' * 2

    def mk(pid, ts, body):
        # 同时给 text_raw/content/idstr：装了平台插件走 hook、
        # 主仓库单独 checkout 走通用兜底，两条路径都能判
        return {'id': pid, 'idstr': pid, 'ts': ts,
                'text_raw': body, 'content': body}

    r1 = await eng.ingest('weibo', 'u1', mk('p1', base, text))
    check('首帖 canonical', r1['decision'] == 'canonical', str(r1))
    # 同池同文不同 pid → 逐字重复
    r2 = await eng.ingest('weibo', 'u1', mk('p2', base + 600, text))
    check('同池同文判重', r2['decision'] == 'duplicate' and r2['reason'] == 'exact', str(r2))
    # 窗口外（+10h）模糊不比、逐字仍命中
    r3 = await eng.ingest('weibo', 'u1', mk('p3', base + 36000,
                                             text + ' 尾巴上多了一点字'))
    check('窗口外近似文不误判', r3['decision'] == 'canonical', str(r3))
    # 窗口内近似文（与任一已见文本都不同字）→ fuzzy
    r4 = await eng.ingest('weibo', 'u1', mk('p4', base + 1200,
                                             text + ' 另一条不一样的尾巴字'))
    check('窗口内近似文 fuzzy 命中', r4['decision'] == 'duplicate'
          and r4['reason'] == 'fuzzy', str(r4))

    # 换源重发号：日志 from_sid 应是旧 sid=1
    reg.upsert_link('weibo', 'u1', 2)
    moved = eng.reissue_for_account('weibo', 'u1', 2)
    check('重发号执行', moved >= 1)
    ev = [e for e in led.read_log(50) if e.get('kind') == 'reissue']
    check('reissue 日志 from_sid=旧源', ev and str(ev[0].get('from_sid')) == '1',
          str(ev[0] if ev else None))
    rec = led.get_alias('weibo', 'p1')
    check('重发号后新旧聚合号都可反查',
          led.aid_index.get(rec['aid']) is rec and
          all(led.aid_index.get(a) is rec for a in rec['aids']))


# ---------------------------------------------------------------- 6. log ring

def test_log_ring(tmp: Path):
    print('[5] 判重日志环形缓冲')
    tmp.mkdir(parents=True, exist_ok=True)
    led = Ledger(str(tmp / 'ldedup'))
    for i in range(50):
        led.log_event({'kind': 'canonical', 'i': i})
    got = led.read_log(20)
    check('ring 读到最新 20 条', len(got) == 20 and got[0]['i'] == 49
          and got[-1]['i'] == 30, f"got {[g['i'] for g in got][:3]}...")
    led2 = Ledger(str(tmp / 'ldedup'))
    check('重启后 ring 预填充', len(led2.read_log(10)) == 10)


async def test_burst_and_video(tmp: Path):
    print('[6] B站视频简介并入指纹 + 同步突发兜底')
    tmp.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    if (root / 'platform' / 'plugin.py').exists():
        from monitor.core.platform import load_plugins
        load_plugins(root / 'platform')
        from sdplat.bili.browser import BrowserManager
        parse_feed_item = BrowserManager.parse_feed_item
        item = {
            "id_str": "1263452367722905618", "type": "DYNAMIC_TYPE_AV",
            "modules": {
                "module_author": {"mid": 161775300, "name": "明日方舟", "pub_ts": 1},
                "module_dynamic": {"desc": {"text": ""},
                    "major": {"archive": {"bvid": "BV1test",
                                           "title": "一分（多）钟看泰拉#5",
                                           "desc": "一包动物饼干的时间了解一个泰拉知识，关注我的频道。",
                                           "cover": "https://i0.hdslb.com/x.jpg"}}},
                "module_stat": {}}}
        d = parse_feed_item(item)
        check('视频简介并入 content', '一包动物饼干' in (d.get('content') or ''),
              repr(d.get('content')))
        check('视频标题保留在 title', d.get('title') == '一分（多）钟看泰拉#5')
    else:
        print('  - 跳过视频简介用例（未安装插件仓）')

    yml = tmp / 'b9.yaml'
    yml.write_text('sources: []' + chr(10), encoding='utf-8')
    reg = SourceRegistry(str(yml), str(tmp / 'breg'))
    reg.create_source(sid=9, name='池九')
    reg.upsert_link('weibo', 'u9', 9)
    reg.upsert_link('bili', 'b9', 9)
    led = Ledger(str(tmp / 'bdedup'))
    cfg = SimpleNamespace(dedup={'enabled': True, 'fuzzy_threshold': 0.6,
                                 'window_hours': 1.0, 'near_miss_low': 0.4,
                                 'sync_burst_window': 60})
    eng = DedupEngine(cfg, reg, led)
    eng.publish_ts = lambda platform, post: float(post['ts'])

    def mk(pid, ts_, body):
        return {'id': pid, 'idstr': pid, 'ts': ts_, 'text_raw': body, 'content': body}

    base = time.time()
    r1 = await eng.ingest('weibo', 'u9', mk('x1', base, '甲平台发布的完全不同的内容甲'))
    check('突发兜底前首发 canonical', r1['decision'] == 'canonical', str(r1))
    r2 = await eng.ingest('bili', 'b9', mk('x2', base + 26, 'totally different text 乙'))
    check('60s 内跨平台同量 → sync_burst 跳过', r2['decision'] == 'duplicate'
          and r2['reason'] == 'sync_burst', str(r2))
    rec = led.get_alias('bili', 'x2')
    check('dup_of 指向先发平台 canonical', (rec.get('dup_of') or {}).get('pid') == 'x1',
          str(rec.get('dup_of')))
    r3 = await eng.ingest('bili', 'b9', mk('x3', base + 3600, '丙 另一段独立内容丙'))
    check('窗口外不受影响', r3['decision'] == 'canonical', str(r3))


async def main():
    tmp = Path(tempfile.mkdtemp(prefix='sd_test_'))
    try:
        test_adfilter_history(tmp / '1')
        test_registry_runtime_overrides(tmp / '2')
        test_ledger_indexes(tmp / '3')
        await test_engine(tmp / '4')
        test_log_ring(tmp / '5')
        await test_burst_and_video(tmp / '6')
        print(f'\n全部通过：{len(PASS)} 项断言')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    asyncio.run(main())
