# -*- coding: utf-8 -*-

"""配置读取 — HubConfig（全局）+ PlatformConfig（每平台视图）

设计目标：平台插件代码几乎零改动。
两侧平台代码统一通过 Config 类的属性访问：browser / monitor / storage /
watchlist / api / logging / development / temp_user_ids / maintenance / get()。
PlatformConfig 把 hub 配置与 platforms.<name> 小节合并成同样的接口视图：

- 通用节（browser / api / logging / development / maintenance）：hub 层提供
  默认值，平台小节里的同名键可覆盖；
- 平台节（monitor / storage / watchlist / temp_user_ids / content_filter）：
  只在平台小节里；
- get(key) 先查平台小节、再回退 hub 根层（兼容平台插件把配置
  config.get('maintenance') 直读的旧用法）。

data 布局（全新生成）：
    data/browser_profile/          全平台共用登录态
    data/<平台>/  登录二维码、watchlist、缓存等（由各插件的小节配置）

    data/logs/   hub.log
    data/restart_schedule.json   全浏览器共用维护计划
"""

import yaml
import os
from pathlib import Path
from typing import Any, Dict


class HubConfig:
    """hub 根配置，直接持有原始 dict"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = Path(config_path)
        self._config = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.config_path}")
        with open(self.config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}

    def reload(self):
        self._config = self._load()

    @property
    def browser(self) -> Dict[str, Any]:
        return self._config.get('browser', {})

    @property
    def api(self) -> Dict[str, Any]:
        return self._config.get('api', {})

    @property
    def logging(self) -> Dict[str, Any]:
        return self._config.get('logging', {})

    @property
    def platforms(self) -> Dict[str, Any]:
        return self._config.get('platforms', {})

    @property
    def maintenance(self) -> Dict[str, Any]:
        return self._config.get('maintenance', {}) or {}

    @property
    def dedup(self) -> Dict[str, Any]:
        """Phase 2 判重/信源配置节"""
        return self._config.get('dedup', {}) or {}

    @property
    def development(self) -> bool:
        return bool(self._config.get('development', False))

    @property
    def external_base_url(self) -> str:
        """对外可达基址（RSS/Atom 图片绝对地址前缀）。

        容器内消费者（bison）经 host.docker.internal 访问宿主，宿主机侧的
        127.0.0.1 对容器无效 —— 用环境变量 SWIFT_DEDUP_PUBLIC_BASE 覆盖：
            set SWIFT_DEDUP_PUBLIC_BASE=http://host.docker.internal:28801
        """
        env = os.environ.get('SWIFT_DEDUP_PUBLIC_BASE', '').strip()
        if env:
            return env.rstrip('/')
        base = self.api.get('external_base_url')
        if not base:
            base = f"http://{self.api.get('host', '127.0.0.1')}:{self.api.get('port', 28801)}"
        return str(base).rstrip('/')

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split('.')
        value: Any = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
                if value is None:
                    return default
            else:
                return default
        return value


class PlatformConfig:
    """平台配置视图：合并 hub 通用节 + platforms.<name> 平台节

    提供与旧项目 Config 完全相同的属性接口，平台代码零改动。
    """

    def __init__(self, name: str, hub: HubConfig):
        self.name = name                       # 平台名
        self._hub = hub
        self._section: Dict[str, Any] = hub.platforms.get(name, {}) or {}
        # data 根：data/<平台名>/（dashboard / 硬编码路径改造的依据）
        self.data_root = Path(f"./data/{name}")

    # ---- 平台专有节 ----

    @property
    def monitor(self) -> Dict[str, Any]:
        return self._section.get('monitor', {}) or {}

    @property
    def storage(self) -> Dict[str, Any]:
        return self._section.get('storage', {}) or {}

    @property
    def watchlist(self) -> Dict[str, Any]:
        return self._section.get('watchlist', {}) or {}

    @property
    def temp_user_ids(self) -> list:
        return self._section.get('temp_user_ids', []) or []

    # ---- hub 通用节（平台可覆盖 browser / api 同名键）----

    @property
    def browser(self) -> Dict[str, Any]:
        merged = dict(self._hub.browser)
        merged.update(self._section.get('browser', {}) or {})
        return merged

    @property
    def api(self) -> Dict[str, Any]:
        # hub 的 api 提供 host / port / external_base_url；
        # 平台小节只放命名覆盖（如 endpoints / auto_subscribe 相关）
        merged = dict(self._hub.api)
        merged.update(self._section.get('api', {}) or {})
        # 本平台路由前缀
        merged.setdefault('prefix', f"/{self.name}")
        return merged

    @property
    def logging(self) -> Dict[str, Any]:
        # 平台无 logging 节，直接沿用 hub（dashboard_level 等读取正常）
        return self._hub.logging

    @property
    def development(self) -> bool:
        return self._hub.development

    @property
    def external_base_url(self) -> str:
        """对外可达基址（平台 RSS/Atom 图片绝对地址前缀），同 hub 规则：
        环境变量 SWIFT_DEDUP_PUBLIC_BASE 可覆盖（容器消费者场景）"""
        return self._hub.external_base_url

    @property
    def maintenance(self) -> Dict[str, Any]:
        return self._hub.maintenance

    def get(self, key: str, default: Any = None) -> Any:
        """先平台小节，后 hub 根层（兼容 config.get('maintenance') 等旧写法）"""
        if key in self._section:
            return self._section[key]
        return self._hub.get(key, default)
