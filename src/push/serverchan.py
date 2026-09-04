"""Server酱通道（推到自己微信）。key 从 SERVERCHAN_SENDKEY 读取。

markdown 内嵌图片需要公网 URL：调用方传入 images(URL列表) 即可附加图片。
"""
from __future__ import annotations

import logging

from src.config import env_secret

logger = logging.getLogger("screener.serverchan")


def send(title: str, desp: str, images: list[str] | None = None) -> bool:
    import requests

    key = env_secret("SERVERCHAN_SENDKEY")
    if not key:
        logger.warning("未配置 SERVERCHAN_SENDKEY，跳过 Server酱")
        return False
    content = desp
    for url in images or []:
        content += f"\n\n![图]({url})"
    try:
        r = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                          data={"title": title, "desp": content}, timeout=30)
        return r.status_code == 200
    except Exception as exc:  # noqa: BLE001
        logger.warning("Server酱发送异常: %s", exc)
        return False
