# model: fingerprint.py — 归一化 / 指纹提取 / 相似度（判重设计规范 §2）
# -*- coding: utf-8 -*-
#
# 原则（对应 spec）：
# - 只比文字（含标题与引用原帖），不比图片；
# - 归一化剥除 URL/短链/@/话题锚/分隔线/标点/emoji/零宽符，保留汉字字母数字；
# - 包含率 = |A∩B|/min(|A|,|B|)（4字符 n-gram 计数袋），对插入/头尾拼接/截断免疫；
# - 阈值经多账号语料校准，回归基准见 scripts/verify_corpus.py。

import hashlib
import re
from collections import Counter

from ..core.platform import attr as _plat_attr

# 阈值（spec §2.3 与 §10 预演：真实镜像对最小 0.84，跨日假对 0.81 由窗口隔离）
FUZZY_THRESHOLD = 0.60
NEAR_MISS_LOW = 0.40
NGRAM_N = 4

# ---------- 归一化 ----------

_RE_URL = re.compile(r'https?://\S+', re.I)
_RE_FWD_HEAD = re.compile(r'//[^\s:：]{1,20}[::]')          # 转发链头 //@xx:
_RE_AT = re.compile(r'@[\w\-\u4e00-\u9fff]+')
_RE_TOPIC = re.compile(r'#.*?(超话)?#')
_RE_SEP = re.compile(r'[\u200b-\u200f\ufeff]|-{5,}|═+|—{2,}|·{2,}|={3,}|━+')
_RE_NONWORD = re.compile(r'[^\w\u4e00-\u9fff]+')
_RE_SPACE = re.compile(r'\s+')


def normalize(text: str) -> str:
    """按 spec §2.1 归一化：剥链接/@/话题/分隔线/标点表情，保留字词数字"""
    t = text or ''
    t = _RE_URL.sub(' ', t)
    t = _RE_FWD_HEAD.sub(' ', t)
    t = _RE_AT.sub(' ', t)
    t = _RE_TOPIC.sub(' ', t)
    t = _RE_SEP.sub(' ', t)
    t = _RE_NONWORD.sub(' ', t)
    return _RE_SPACE.sub(' ', t).strip()


# ---------- 主文激进清理（仅指纹用，不影响任何展示，spec D18） ----------

_SEP_HARD = re.compile(r'^[\u2014\u2500\u2501\u2015]{4,}\s*$')     # —— / ── 类
_SEP_SOFT = re.compile(r'^[_\uFF3F\-\u2010\u2012]{4,}\s*$')        # ____ / ---- 类
_TAG_TOKEN = re.compile(r'#[^#\n]{1,60}#')


def _is_tag_only_line(line: str) -> bool:
    """整行只是 话题# 串（超话/井号），可含空白；至少有一个 # 剩余非标签字符为空"""
    t = (line or '').strip()
    if not t or '#' not in t:
        return False
    residue = _TAG_TOKEN.sub('', t)
    return residue.strip('# \u3000\t') == ''


def aggressive_main(text: str) -> str:
    """①丢掉正文中开头的纯话题行；②第一条横线分隔行及其下全部删除。

    只作用于"主文"（引用部分另行合并），展示层文本永不经过本函数。
    """
    lines = (text or '').splitlines()
    while lines and _is_tag_only_line(lines[0]):
        lines = lines[1:]
    out = []
    for ln in lines:
        s = ln.strip()
        if _SEP_HARD.match(s) or _SEP_SOFT.match(s):
            break
        out.append(ln)
    return '\n'.join(out)


# ---------- 指纹文本提取（spec §2.2 + D18/D20） ----------

def _merge_title_content(title: str, content: str) -> str:
    """二者包含时取长者，避免重复计数"""
    title, content = (title or '').strip(), (content or '').strip()
    if not title:
        return content
    if not content:
        return title
    if title in content or content in title:
        return title if len(title) >= len(content) else content
    return f'{title} {content}'


def extract_text(platform: str, post: dict) -> str:
    """按平台插件提取指纹文本 = 激进清理后的主文 + 引用原文"""
    fn = _plat_attr(platform, 'fingerprint_text')
    if fn is not None:
        return fn(post)
    # 未注册平台：基于 title/content 的通用兜底
    main = aggressive_main(_merge_title_content(post.get('title') or '',
                                                post.get('content') or ''))
    return main


# ---------- 指纹与相似度 ----------

def exact_hash(normalized: str) -> str:
    """逐字级指纹哈希（无时间限制判重用）"""
    return hashlib.sha1(normalized.encode('utf-8')).hexdigest()


def ngram_bags(normalized: str):
    """4 字符 n-gram 计数袋；空串返回 None（调用方视为不可比）"""
    s = normalized.replace(' ', '')
    if not s:
        return None
    n = NGRAM_N
    if len(s) <= n:
        return Counter([s])
    return Counter(s[i:i + n] for i in range(len(s) - n + 1))


def containment(bag_a: Counter, bag_b: Counter) -> float:
    """包含率 |A∩B| / min(|A|,|B|)；双方都要有值"""
    if not bag_a or not bag_b:
        return 0.0
    inter = sum((bag_a & bag_b).values())
    return inter / max(1, min(sum(bag_a.values()), sum(bag_b.values())))


def bags_to_json(stack) -> list:
    """Counter -> 可序列化列表（存 jsonl 用）"""
    return [[g, c] for g, c in stack.items()]


def bags_from_json(pairs) -> Counter:
    return Counter(dict(pairs or []))


def is_near_miss(score: float) -> bool:
    """影子期盯防段（spec §2.3）"""
    return NEAR_MISS_LOW <= score < FUZZY_THRESHOLD


def is_hit(score: float) -> bool:
    return score >= FUZZY_THRESHOLD
