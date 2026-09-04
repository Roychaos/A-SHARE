"""网络辅助：脚本启动时按配置决定是否忽略系统代理。

背景：akshare 底层 requests 会自动读取 HTTP_PROXY/HTTPS_PROXY 环境变量。
若本机设置过 Clash/VPN 等代理但未开启或不允许访问国内行情站点，会导致
ProxyError。东方财富/新浪等行情站本应直连，因此默认剥离代理变量。
"""
from __future__ import annotations

import os

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
               "http_proxy", "https_proxy", "all_proxy")


def sanitize_proxy_env() -> None:
    """从当前进程环境变量中移除全部代理设置（只影响本进程）。"""
    for var in _PROXY_VARS:
        os.environ.pop(var, None)
