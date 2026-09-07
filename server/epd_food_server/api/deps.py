"""路由共享的小工具（避免循环导入放在独立模块）。"""

from __future__ import annotations


def notify_change(request) -> None:
    """写操作后请求一次防抖推送；可在任意线程调用（同步端点跑在线程池）。"""
    pusher = getattr(request.app.state, "pusher", None)
    loop = getattr(request.app.state, "loop", None)
    if pusher is None or loop is None:
        return
    loop.call_soon_threadsafe(pusher.request_change_push)
