# -*- coding: utf-8 -*-

"""桌面通知 — 当前用于扫码登录提醒（plyer）

SwiftDedup: 两平台的浏览器登录代码都调用 notify_login_needed，签名与它们
的旧版本兼容（只有 qr_path 一个必填参数），app_name 可在需要时传入。
"""

from loguru import logger


def _notify(title: str, message: str, app_name: str = "SwiftDedup"):
    try:
        from plyer import notification
        notification.notify(
            title=title,
            message=message,
            app_name=app_name,
            timeout=10,
        )
        logger.info(f"📢 桌面通知已发送: {title}")
    except Exception as e:
        logger.warning(f"桌面通知发送失败: {e}")


def notify_login_needed(qr_path: str = "./data/qrcode.png", app_name: str = "SwiftDedup"):
    """平台登录需要扫码时弹出通知"""
    _notify(
        title="SwiftDedup — 需要登录",
        message=f"请在终端扫码登录\n二维码图片: {qr_path}",
        app_name=app_name,
    )
