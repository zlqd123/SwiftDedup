# -*- coding: utf-8 -*-

"""每日维护 — 窗口内随机时刻重启浏览器（只重启浏览器进程，爬虫进程不动）

多平台共用的每日维护调度，现在收编到 hub：
- 摇号计划（精确到秒，避开整点/半点前后 guard 分钟）持久化到
  data/restart_schedule.json，跨天自动重摇，全浏览器共用一份；
- 重启前置条件改为全平台投票：每个平台 backend.prepare_restart() 都为 True
  （无待处理新动态信号）才执行；窗口快结束时等不到一致则放弃当日重启；
- 重启执行后逐平台 ack_restart / relogin 重新登录恢复监控页；
- 计划同时回写到各平台 backend 的 _restart_* 展示字段，网页面板照常显示。

各平台的维护配置不再读平台小节，统一在 config.yaml 的 maintenance 节。
"""

import json
import random
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional

from loguru import logger


class MaintenanceScheduler:
    """摇号 + 到点判定 + 全平台一致的准备投票"""

    def __init__(self, config, backends: Dict[str, "BrowserManager"], hub_browser):
        self.config = config
        self.backends = backends            # platform -> BrowserManager（各平台已适配版）
        self.hub = hub_browser              # HubBrowser：唯一持有 playwright/context
        self._schedule_file = Path("./data/restart_schedule.json")
        br = (config.maintenance or {}).get('browser_restart', {}) or {}
        self._enabled = bool(br.get('enabled', False))
        self._window_start = int(br.get('window_start', 5))
        self._window_end = int(br.get('window_end', 7))
        self._guard_minutes = int(br.get('guard_minutes', 5))
        self._date = ""
        self._today: Optional[datetime] = None
        self._done = False
        # 展示字段回写
        for b in self.backends.values():
            b._restart_enabled = self._enabled

    # ---------- 摇号 ----------

    def _valid_minute(self, minute: int) -> bool:
        """离整点/半点前后 guard 分钟之外的分钟才可作为重启时刻"""
        g = self._guard_minutes
        return not (minute <= g or minute >= 60 - g or abs(minute - 30) <= g)

    def _roll_target(self):
        """在 [window_start, window_end) 内摇一个合法时刻；候选全已过点则当天跳过"""
        now = datetime.now()
        candidates = []
        for hour in range(self._window_start, self._window_end):
            for minute in range(60):
                if self._valid_minute(minute):
                    t = now.replace(hour=hour, minute=minute,
                                    second=random.randint(0, 59), microsecond=0)
                    if t > now:
                        candidates.append(t)
        return random.choice(candidates) if candidates else None

    def _save(self):
        try:
            self._schedule_file.parent.mkdir(parents=True, exist_ok=True)
            self._schedule_file.write_text(
                json.dumps({
                    "date": self._date,
                    "target": self._today.isoformat() if self._today else None,
                    "done": self._done,
                }, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.debug(f"维护计划落盘失败: {e}")

    def _load_or_roll(self, today: str):
        """跨天（或文件损坏/缺失）时重新摇号并落盘"""
        target = self._roll_target()
        self._date = today
        self._today = target
        self._done = target is None   # 无合法时刻=当天跳过
        self._save()
        if target:
            logger.info(f"🗓 今日浏览器维护已摇号: {target.strftime('%H:%M:%S')}")
        else:
            logger.info("🗓 今日维护窗口内无可用时刻（或启动时已过），当天跳过")
        self._publish()

    def _publish(self):
        """把计划回写到各平台 backend 的展示字段（dashboards 直接可读）"""
        iso = self._today.isoformat() if self._today else None
        for b in self.backends.values():
            b._restart_today = iso

    # ---------- 对外入口 ----------

    async def tick(self) -> bool:
        """扫描循环每轮调用；到点且全平台都安全时执行一次整体重启。

        返回 True 表示发生了重启。
        """
        if not self._enabled:
            return False
        today = datetime.now().strftime("%Y-%m-%d")
        if self._date != today:
            # 先沿用文件里的计划；跨天或缺失才重新摇号
            if self._schedule_file.exists():
                try:
                    data = json.loads(self._schedule_file.read_text(encoding="utf-8"))
                    if data.get("date") == today:
                        self._date = today
                        t = data.get("target")
                        self._today = datetime.fromisoformat(t) if t else None
                        self._done = bool(data.get("done"))
                        self._publish()
                except Exception:
                    pass
            if self._date != today:
                self._load_or_roll(today)
        if self._done or self._today is None:
            return False
        if datetime.now() < self._today:
            return False

        # 全平台投票：任一平台有未处理信号 → 顺延到下一轮 tick
        ready = {}
        for name, backend in self.backends.items():
            try:
                ok = await backend.prepare_restart()
            except Exception as e:
                logger.debug(f"[维护] {name} prepare_restart 异常: {e}")
                ok = False
            ready[name] = ok
        if not all(ready.values()):
            logger.debug("[维护] 平台未就绪，顺延: "
                         + ", ".join(f"{n}={'✓' if ok else '✗'}" for n, ok in ready.items()))
            return False

        # 窗口尾保护：目标时刻已过 30 分钟仍无法达成一致则放弃当日（防止深夜反复重试）
        graced = (datetime.now() - self._today).total_seconds()
        if graced > 30 * 60:
            logger.warning(f"🗓 维护时刻已过 {graced / 60:.0f} 分钟仍未达成一致，跳过今日维护")
            self._done = True
            self._save()
            self._publish()
            return False

        logger.info(f"🔄 每日维护：到达摇号时刻 {self._today.strftime('%H:%M:%S')}，"
                    "仅重启浏览器进程（爬虫进程不重启）")
        try:
            await self._do_restart()
        except Exception as e:
            logger.error(f"🔄 维护重启执行失败: {e}")
        finally:
            self._done = True
            self._save()
            self._publish()
        return True

    async def _do_restart(self):
        if self.hub is None:
            logger.error("[维护] 找不到 HubBrowser，跳过重启")
            return
        await self.hub.restart()
        for name, backend in self.backends.items():
            try:
                await backend.ack_restart()
            except Exception as e:
                # 无人值守时段登录可能需要扫码失败属正常；下一轮看门狗/扫描兜底
                logger.error(f"[维护] {name} 重启后重登录失败: {e}")
        logger.info("🔄 浏览器重启完成")
