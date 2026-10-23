# -*- coding: utf-8 -*-

"""平台插件注册表 + 目录自动发现

框架自身零平台知识：所有具体平台由 platform/ 目录下的插件提供。

插件形态：把平台代码仓库 clone 为 SwiftDedup/platform/（或其一级子目录），
目录内存在 plugin.py 即被识别。plugin.py 通过相对 import 携带兄弟包，
加载时调用 register(plugin) 或暴露模块级 PLUGIN。

Plugin 需提供的属性/方法（duck protocol）：
    name, display_name, domains, manual_url
    create_service(platform_config, hub_browser)
    publish_ts(post) -> float
    extract_pid(post) -> str
    fingerprint_text(post) -> str          # 判重输入文本
    display_text(post) -> str              # 管理页展示文本
    forward_note(post) -> str              # 转发帖补引（无则空串）
    lottery_filtered(post) -> bool         # 抽奖公示过滤
    image_urls(post, base) -> list[str]    # 管理页图片地址
    post_link(uid, post) -> (link, author)
    iter_seed_cache(path) -> 迭代器(lat, uid, post)   # 历史播种
    tag_css -> str                         # 可选：管理页平台标签配色
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List

_PLUGINS: Dict[str, Any] = {}
_DISPLAY: Dict[str, str] = {}
_DISPLAY['sd'] = 'SwiftDedup'          # 框架自身的聚合页
DISPLAY_NAME = _DISPLAY               # 引擎/管理页沿用旧名（同一对象，注册即更新）


def register(plugin) -> None:
    name = str(getattr(plugin, 'name', '') or '').strip()
    if not name:
        raise ValueError('plugin.name 缺失')
    _PLUGINS[name] = plugin
    disp = str(getattr(plugin, 'display_name', '') or '')
    if disp:
        _DISPLAY[name] = disp


def get(name: str):
    return _PLUGINS.get(name)


def has(name: str) -> bool:
    return name in _PLUGINS


def names() -> List[str]:
    return list(_PLUGINS.keys())


def display_name(name: str) -> str:
    return _DISPLAY.get(name) or name


def display_map() -> Dict[str, str]:
    return dict(_DISPLAY)


def attr(name: str, attribute: str, default: Any = None):
    plugin = _PLUGINS.get(name)
    return getattr(plugin, attribute, default)


def _load_module(pkg_name: str, root: Path):
    plugin_py = root / 'plugin.py'
    if pkg_name in sys.modules:
        return sys.modules[pkg_name]
    spec = importlib.util.spec_from_file_location(
        pkg_name, str(plugin_py), submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = mod
    spec.loader.exec_module(mod)
    fn = getattr(mod, 'register', None)
    if callable(fn) and fn is not register:      # 防止把 import 进来的 register 当入口
        fn(register)
    else:
        p = getattr(mod, 'PLUGIN', None)
        if p is not None:
            register(p)
    return mod


def load_plugins(root) -> int:
    """扫描 platform/ 目录并加载全部插件；返回本次新注册的平台数"""
    root = Path(root)
    if not root.is_dir():
        return 0
    before = len(_PLUGINS)
    if (root / 'plugin.py').exists():
        try:
            _load_module('sdplat', root)
        except Exception as e:
            print(f'[平台插件] 加载失败 {root}: {e!r}')
    for sub in sorted(root.iterdir()):
        if (sub.is_dir() and not sub.name.startswith('.')
                and not sub.name.startswith('__')
                and (sub / 'plugin.py').exists()):
            try:
                _load_module('sdplat_' + ''.join(
                    ch for ch in sub.name.lower() if ch.isalnum()), sub)
            except Exception as e:
                print(f'[平台插件] 加载失败 {sub}: {e!r}')
    return len(_PLUGINS) - before
